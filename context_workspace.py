#!/usr/bin/env python3
"""Generate MySQL metadata context and use Gemini for aggregate SQL workflows."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


PLAN_SEPARATOR = "---PLAN-ITEM---"
LAST_GEMINI_CALL_AT = 0.0


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str | None


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model: str


@dataclass(frozen=True)
class PlanItem:
    index: int
    title: str
    aggregation: str
    visual_type: str
    raw_text: str


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_db_config(args: argparse.Namespace) -> DbConfig:
    load_dotenv()
    return DbConfig(
        host=getattr(args, "host", None) or os.getenv("DB_HOST", "87.110.123.151"),
        port=int(getattr(args, "port", None) or os.getenv("DB_PORT", "3306")),
        user=getattr(args, "user", None) or os.getenv("DB_USER", "fita"),
        password=getattr(args, "password", None) or os.getenv("DB_PASSWORD", ""),
        database=getattr(args, "database", None) or os.getenv("DB_NAME") or None,
    )


def read_gemini_config(args: argparse.Namespace) -> GeminiConfig:
    load_dotenv()
    return GeminiConfig(
        api_key=getattr(args, "gemini_api_key", None) or os.getenv("GEMINI_API_KEY", ""),
        model=getattr(args, "gemini_model", None) or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    )


def connect(config: DbConfig):
    try:
        import mysql.connector
    except ImportError as exc:
        raise SystemExit(
            "Trukst mysql-connector-python. Palaid: pip install -r requirements.txt"
        ) from exc

    if not config.password:
        raise SystemExit("Nav noradita MySQL parole. Ievadi DB_PASSWORD .env faila.")

    connection_args: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "password": config.password,
        "connection_timeout": 10,
    }
    if config.database:
        connection_args["database"] = config.database
    return mysql.connector.connect(**connection_args)


def fetch_all(cursor, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor.execute(query, params)
    return list(cursor.fetchall())


def list_databases(connection) -> list[str]:
    cursor = connection.cursor(dictionary=True)
    rows = fetch_all(
        cursor,
        """
        SELECT SCHEMA_NAME AS name
        FROM information_schema.SCHEMATA
        WHERE SCHEMA_NAME NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
        ORDER BY SCHEMA_NAME
        """,
    )
    cursor.close()
    return [row["name"] for row in rows]


def get_tables(cursor, database: str, selected_tables: list[str] | None) -> list[dict[str, Any]]:
    params: list[Any] = [database]
    table_filter = ""
    if selected_tables:
        placeholders = ", ".join(["%s"] * len(selected_tables))
        table_filter = f" AND TABLE_NAME IN ({placeholders})"
        params.extend(selected_tables)

    return fetch_all(
        cursor,
        f"""
        SELECT TABLE_NAME AS name, TABLE_TYPE AS type, TABLE_COMMENT AS comment
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s{table_filter}
        ORDER BY TABLE_NAME
        """,
        tuple(params),
    )


def get_columns(cursor, database: str, table: str) -> list[dict[str, Any]]:
    return fetch_all(
        cursor,
        """
        SELECT
            COLUMN_NAME AS name,
            COLUMN_TYPE AS data_type,
            IS_NULLABLE AS nullable,
            COLUMN_KEY AS column_key,
            COLUMN_DEFAULT AS default_value,
            EXTRA AS extra,
            COLUMN_COMMENT AS comment
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (database, table),
    )


def get_constraints(cursor, database: str, table: str) -> list[dict[str, Any]]:
    return fetch_all(
        cursor,
        """
        SELECT
            tc.CONSTRAINT_NAME AS constraint_name,
            tc.CONSTRAINT_TYPE AS constraint_type,
            kcu.COLUMN_NAME AS column_name,
            kcu.REFERENCED_TABLE_NAME AS referenced_table,
            kcu.REFERENCED_COLUMN_NAME AS referenced_column
        FROM information_schema.TABLE_CONSTRAINTS tc
        JOIN information_schema.KEY_COLUMN_USAGE kcu
          ON tc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
         AND tc.TABLE_NAME = kcu.TABLE_NAME
         AND tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
        WHERE tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
        ORDER BY tc.CONSTRAINT_TYPE, tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """,
        (database, table),
    )


def describe_column_constraints(column: dict[str, Any], constraints: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    if column["nullable"] == "NO":
        parts.append("NOT NULL")
    if column["default_value"] is not None:
        parts.append(f"DEFAULT {column['default_value']}")
    if column["extra"]:
        parts.append(str(column["extra"]).upper())

    for constraint in constraints:
        if constraint["column_name"] != column["name"]:
            continue
        constraint_type = constraint["constraint_type"]
        if constraint_type == "FOREIGN KEY":
            parts.append(
                f"FOREIGN KEY -> {constraint['referenced_table']}.{constraint['referenced_column']}"
            )
        else:
            parts.append(constraint_type)

    return ", ".join(dict.fromkeys(parts)) or "-"


def markdown_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    return text[:160] + "..." if len(text) > 160 else text


def markdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_Nav ierakstu._"
    headers = list(rows[0].keys())
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        values = [markdown_value(row.get(header)).replace("|", "\\|") for header in headers]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def generate_context(connection, database: str, selected_tables: list[str] | None) -> str:
    cursor = connection.cursor(dictionary=True)
    cursor.execute(f"USE `{database}`")
    tables = get_tables(cursor, database, selected_tables)

    lines = [
        f"# MySQL servera datu strukturas konteksts: `{database}`",
        "",
        "Sis konteksts apraksta tikai datubazes strukturu, nevis tabulu datus.",
        "",
        "## Kopsavilkums",
        "",
        f"- Datubaze: `{database}`",
        f"- Tabulu skaits: {len(tables)}",
        "",
        "## Tabulas un kolonnas",
        "",
    ]

    for table in tables:
        table_name = table["name"]
        constraints = get_constraints(cursor, database, table_name)
        columns = get_columns(cursor, database, table_name)
        column_rows = [
            {
                "kolonna": column["name"],
                "datu_tips": column["data_type"],
                "ierobezojumi": describe_column_constraints(column, constraints),
                "komentars": column["comment"] or "-",
            }
            for column in columns
        ]

        lines.extend(
            [
                f"### `{table_name}`",
                "",
                f"- Tips: {table['type']}",
            ]
        )
        if table.get("comment"):
            lines.append(f"- Komentars: {table['comment']}")
        lines.extend(["", markdown_table(column_rows), ""])

    cursor.close()
    return "\n".join(lines).strip() + "\n"


def call_gemini(config: GeminiConfig, prompt: str) -> str:
    try:
        import requests
    except ImportError as exc:
        raise SystemExit("Trukst requests. Palaid: pip install -r requirements.txt") from exc

    if not config.api_key:
        raise SystemExit("Nav noradita GEMINI_API_KEY .env faila.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.model}:generateContent"
    global LAST_GEMINI_CALL_AT
    response = None
    last_error: Exception | None = None
    for attempt in range(1, 4):
        min_interval = float(os.getenv("GEMINI_MIN_INTERVAL_SECONDS", "13"))
        elapsed = time.monotonic() - LAST_GEMINI_CALL_AT
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        try:
            response = requests.post(
                url,
                headers={"x-goog-api-key": config.api_key, "Content-Type": "application/json"},
                json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
                timeout=60,
            )
            LAST_GEMINI_CALL_AT = time.monotonic()
        except requests.RequestException as exc:
            last_error = exc
            response = None
            if attempt < 3:
                time.sleep(5 * attempt)
                continue
            break
        if response.status_code not in {429, 500, 502, 503, 504}:
            break
        if attempt < 3:
            time.sleep(65 if response.status_code == 429 else 5 * attempt)
    if response is None:
        raise SystemExit(f"Gemini API neatgrieza atbildi: {last_error}")
    if not response.ok:
        raise SystemExit(
            f"Gemini API kluda ({response.status_code}). Atbilde:\n{response.text}"
        )
    payload = response.json()
    parts = payload["candidates"][0]["content"]["parts"]
    return "\n".join(part.get("text", "") for part in parts).strip()


def extract_sql(text: str) -> str:
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    sql = match.group(1).strip() if match else text.strip()
    return sql.rstrip(";").strip() + ";"


def validate_select_sql(sql: str) -> None:
    normalized = sql.strip().lower()
    forbidden = [
        "insert ",
        "update ",
        "delete ",
        "drop ",
        "alter ",
        "create ",
        "truncate ",
        "replace ",
        "grant ",
        "revoke ",
    ]
    if not normalized.startswith(("select", "with")):
        raise SystemExit("Drosibas del drikst izpildit tikai SELECT vai WITH vaicajumus.")
    if any(token in normalized for token in forbidden):
        raise SystemExit("SQL satur neatlautu datu mainisanas vai strukturas komandu.")


def json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: str, content: str) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")


def generate_plan_text(gemini: GeminiConfig, context: str, question: str, item_count: int) -> str:
    prompt = f"""
Tu esi datu analitikas plansanas asistents. Izmanto tikai zemak doto MySQL datubazes strukturas kontekstu.
Izveido analitikas planu, ko velak var izpildit ar SQL un Python vizualizacijam.

Noteikumi:
- Izveido {item_count} plana punktus.
- Katram punktam jabut realistiski izpildamam ar agregatu SQL no dotajam tabulam.
- Katram punktam noradi datu apkopojumu, ko vizualizet.
- Katram punktam noradi vienu vizuala tipu: bar, line, pie, scatter vai table.
- Neizdomat kolonnas vai tabulas, kas nav konteksta.
- Starp plana punktiem obligati izmanto atsevisku rindu ar tekstu: {PLAN_SEPARATOR}
- Neizmanto Markdown tabulas.
- Katrs plana punkts ir saja forma:
Nosaukums: ...
Datu apkopojums: ...
Vizuala tips: ...
Pamatojums: ...

Lietotaja merkis:
{question}

Datubazes konteksts:
{context}
""".strip()
    return call_gemini(gemini, prompt).strip() + "\n"


def parse_labeled_value(text: str, labels: list[str]) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        for label in labels:
            if line.lower().startswith(label.lower() + ":"):
                return line.split(":", 1)[1].strip()
    return None


def parse_plan(plan_text: str) -> list[PlanItem]:
    chunks = [chunk.strip() for chunk in plan_text.split(PLAN_SEPARATOR) if chunk.strip()]
    items: list[PlanItem] = []
    for index, chunk in enumerate(chunks, start=1):
        title = parse_labeled_value(chunk, ["Nosaukums", "Title"]) or f"Plana punkts {index}"
        aggregation = (
            parse_labeled_value(chunk, ["Datu apkopojums", "Apkopojums", "Aggregation"])
            or chunk
        )
        visual_type = (
            parse_labeled_value(chunk, ["Vizuala tips", "Visual type", "Chart type"]) or "bar"
        )
        items.append(
            PlanItem(
                index=index,
                title=title,
                aggregation=aggregation,
                visual_type=visual_type,
                raw_text=chunk,
            )
        )
    if not items:
        raise SystemExit(
            f"Plana fails nesatur punktus. Parbaudi, vai izmantots atdalitajs {PLAN_SEPARATOR}."
        )
    return items


def generate_sql_for_plan_item(gemini: GeminiConfig, context: str, item: PlanItem) -> str:
    prompt = f"""
Tu esi datu analitikas asistents. Izmanto tikai zemak doto MySQL datubazes strukturas kontekstu.
Uzraksti vienu MySQL SELECT vaicajumu, kas izgus datus sim plana punktam.

Noteikumi:
- Atbilde satur tikai SQL kodu, bez paskaidrojumiem.
- Neizmanto INSERT, UPDATE, DELETE, DROP, ALTER, CREATE vai citas datu/strukturas mainisanas komandas.
- Vaicajumam jabut agregatam vai kopsavilkumam, kas der vizualizacijai.
- Ierobezo rezultatu apjomu ar ORDER BY un LIMIT, ja var but daudz grupu.
- Neatlasi visas neapstradatas rindas.

Plana punkts:
{item.raw_text}

Datubazes konteksts:
{context}
""".strip()
    sql = extract_sql(call_gemini(gemini, prompt))
    validate_select_sql(sql)
    return sql


def run_sql_text(connection, sql: str, max_rows: int) -> dict[str, Any]:
    validate_select_sql(sql)
    cursor = connection.cursor(dictionary=True, buffered=True)
    try:
        cursor.execute(sql)
        rows = cursor.fetchmany(max_rows)
        return {
            "sql": sql,
            "max_rows": max_rows,
            "rows": [{key: json_safe(value) for key, value in row.items()} for row in rows],
        }
    finally:
        cursor.close()


def normalize_visual_type(visual_type: str) -> str:
    text = visual_type.strip().lower()
    if any(token in text for token in ["line", "lin", "trend", "laika"]):
        return "line"
    if any(token in text for token in ["pie", "aplis", "donut"]):
        return "pie"
    if any(token in text for token in ["scatter", "izklied"]):
        return "scatter"
    if any(token in text for token in ["table", "tabul"]):
        return "table"
    return "bar"


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip().replace(",", ".")
        return float(text)
    except (TypeError, ValueError):
        return None


def numeric_columns(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    columns = list(rows[0].keys())
    numeric: list[str] = []
    for column in columns:
        values = [as_float(row.get(column)) for row in rows if row.get(column) is not None]
        if values and len(values) == sum(value is not None for value in values):
            numeric.append(column)
    return numeric


def label_column(rows: list[dict[str, Any]], excluded: set[str]) -> str | None:
    if not rows:
        return None
    for column in rows[0].keys():
        if column not in excluded:
            return column
    return None


def create_visualization(result: dict[str, Any], item: PlanItem, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mpl_config_dir = output_path.parent / ".matplotlib"
    mpl_config_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(mpl_config_dir))

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit("Trukst matplotlib. Palaid: pip install -r requirements.txt") from exc

    rows = result.get("rows", [])
    chart_type = normalize_visual_type(item.visual_type)

    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=140)
    fig.patch.set_facecolor("white")
    ax.set_title(item.title, fontsize=13, pad=14)

    if not rows:
        ax.axis("off")
        ax.text(0.5, 0.5, "Nav datu vizualizacijai", ha="center", va="center", fontsize=12)
        fig.tight_layout()
        fig.savefig(output_path, bbox_inches="tight")
        plt.close(fig)
        return

    plot_rows = rows[:30]
    numeric = numeric_columns(plot_rows)

    if chart_type == "table" or not numeric:
        ax.axis("off")
        columns = list(plot_rows[0].keys())
        table_values = [[markdown_value(row.get(column)) for column in columns] for row in plot_rows]
        table = ax.table(
            cellText=table_values,
            colLabels=columns,
            loc="center",
            cellLoc="left",
            colLoc="left",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        table.scale(1, 1.35)
        fig.tight_layout()
        fig.savefig(output_path, bbox_inches="tight")
        plt.close(fig)
        return

    if len(plot_rows) == 1 and len(numeric) > 1:
        labels = numeric
        values = [as_float(plot_rows[0].get(column)) or 0 for column in numeric]
        y_label = "Vertiba"
    else:
        y_column = numeric[0]
        x_column = label_column(plot_rows, {y_column}) or y_column
        labels = [markdown_value(row.get(x_column)) for row in plot_rows]
        values = [as_float(row.get(y_column)) or 0 for row in plot_rows]
        y_label = y_column

    if chart_type == "line":
        ax.plot(labels, values, marker="o", linewidth=2)
        ax.set_ylabel(y_label)
        ax.grid(axis="y", alpha=0.25)
    elif chart_type == "pie" and values and all(value >= 0 for value in values):
        ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
        ax.axis("equal")
    elif chart_type == "scatter" and len(numeric) >= 2:
        x_column, y_column = numeric[:2]
        x_values = [as_float(row.get(x_column)) or 0 for row in plot_rows]
        y_values = [as_float(row.get(y_column)) or 0 for row in plot_rows]
        ax.scatter(x_values, y_values, s=55)
        ax.set_xlabel(x_column)
        ax.set_ylabel(y_column)
        ax.grid(alpha=0.25)
    else:
        ax.bar(labels, values)
        ax.set_ylabel(y_label)
        ax.grid(axis="y", alpha=0.25)

    if chart_type in {"bar", "line"}:
        ax.tick_params(axis="x", rotation=35)
        for label in ax.get_xticklabels():
            label.set_horizontalalignment("right")

    fig.tight_layout()
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def describe_visual(gemini: GeminiConfig, context: str, item: PlanItem, result: dict[str, Any]) -> str:
    prompt = f"""
Tu esi datu analitikas asistents. Apraksti vizuali un dod konkretus ieskatus latviesu valoda.
Balsties tikai uz datubazes strukturas kontekstu, plana punktu, SQL un rezultatu JSON.

Atbilde:
- 1 teikums, ko vizualis rada;
- 2-4 konkreti ieskati par redzamajiem datiem;
- piesardzibas piezime, ja metadati nepasaka biznesa nozimi.

Plana punkts:
{item.raw_text}

Datubazes konteksts:
{context}

SQL:
{result.get("sql", "")}

Rezultati JSON:
{json.dumps(result, ensure_ascii=False, indent=2)}
""".strip()
    return call_gemini(gemini, prompt).strip()


def html_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>Nav ierakstu.</p>"
    columns = list(rows[0].keys())
    header = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    body_rows = []
    for row in rows[:20]:
        cells = "".join(
            f"<td>{html.escape(markdown_value(row.get(column)))}</td>" for column in columns
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def build_report_html(
    database: str,
    question: str,
    plan_text: str,
    processed_items: list[dict[str, Any]],
    output_path: Path,
) -> str:
    item_sections: list[str] = []
    for processed in processed_items:
        item = processed["item"]
        image_path = Path(processed["image_path"])
        relative_image = os.path.relpath(image_path, start=output_path.parent)
        result = processed["result"]
        item_sections.append(
            f"""
<section class="visual">
  <h2>{item.index}. {html.escape(item.title)}</h2>
  <p class="meta"><strong>Datu apkopojums:</strong> {html.escape(item.aggregation)}<br>
  <strong>Vizuala tips:</strong> {html.escape(item.visual_type)}</p>
  <img src="{html.escape(relative_image)}" alt="{html.escape(item.title)}">
  <div class="description">{markdown_to_simple_html(processed["description"])}</div>
  <details>
    <summary>SQL un dati</summary>
    <pre><code>{html.escape(result.get("sql", ""))}</code></pre>
    {html_table(result.get("rows", []))}
  </details>
</section>
""".strip()
        )

    return f"""
<!doctype html>
<html lang="lv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Datu analitikas atskaite</title>
  <style>
    :root {{
      color-scheme: light;
      --text: #1f2933;
      --muted: #5f6b7a;
      --border: #d9e2ec;
      --accent: #176b87;
      --bg: #f7f9fb;
      --panel: #ffffff;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
      line-height: 1.55;
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}
    header, section {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 24px;
      margin-bottom: 18px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
      line-height: 1.2;
    }}
    h1 {{
      font-size: 30px;
    }}
    h2 {{
      font-size: 22px;
    }}
    .meta {{
      color: var(--muted);
      margin: 0 0 16px;
    }}
    img {{
      display: block;
      width: 100%;
      max-width: 100%;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: white;
    }}
    pre {{
      overflow-x: auto;
      background: #0f172a;
      color: #e2e8f0;
      padding: 14px;
      border-radius: 6px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
      font-size: 14px;
    }}
    th, td {{
      border: 1px solid var(--border);
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #eef3f8;
    }}
    summary {{
      cursor: pointer;
      color: var(--accent);
      font-weight: 600;
      margin-top: 14px;
    }}
    .description ul {{
      margin-top: 8px;
    }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>Datu analitikas atskaite</h1>
    <p class="meta"><strong>Datubaze:</strong> {html.escape(database)}<br>
    <strong>Merkis:</strong> {html.escape(question)}</p>
  </header>
  <section>
    <h2>Plans</h2>
    <pre><code>{html.escape(plan_text)}</code></pre>
  </section>
  {"".join(item_sections)}
</main>
</body>
</html>
""".strip() + "\n"


def markdown_to_simple_html(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    html_lines: list[str] = []
    in_list = False
    for line in lines:
        if line.startswith(("- ", "* ")):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{html.escape(line[2:].strip())}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        html_lines.append("</ul>")
    return "\n".join(html_lines)


def process_plan_to_report(
    db_config: DbConfig,
    gemini: GeminiConfig,
    context: str,
    plan_text: str,
    question: str,
    output: str,
    max_rows: int,
) -> None:
    output_path = Path(output)
    assets_dir = output_path.parent / f"{output_path.stem}_assets"
    items = parse_plan(plan_text)
    connection = connect(db_config)
    processed_items: list[dict[str, Any]] = []
    try:
        for item in items:
            prefix = f"item_{item.index:02d}"
            sql = generate_sql_for_plan_item(gemini, context, item)
            result = run_sql_text(connection, sql, max_rows)
            image_path = assets_dir / f"{prefix}.png"
            create_visualization(result, item, image_path)
            description = describe_visual(gemini, context, item, result)

            write_text(str(assets_dir / f"{prefix}.sql"), sql + "\n")
            write_text(
                str(assets_dir / f"{prefix}.json"),
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            )
            write_text(str(assets_dir / f"{prefix}.md"), description + "\n")
            processed_items.append(
                {
                    "item": item,
                    "result": result,
                    "image_path": image_path,
                    "description": description,
                }
            )
            print(f"Apstradats plana punkts {item.index}: {item.title}")
    finally:
        connection.close()

    report = build_report_html(
        database=db_config.database or "",
        question=question,
        plan_text=plan_text,
        processed_items=processed_items,
        output_path=output_path,
    )
    write_text(str(output_path), report)


def command_list_databases(args: argparse.Namespace) -> int:
    config = read_db_config(args)
    connection = connect(
        DbConfig(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=None,
        )
    )
    try:
        print("\n".join(list_databases(connection)))
        return 0
    finally:
        connection.close()


def command_context(args: argparse.Namespace) -> int:
    config = read_db_config(args)
    if not config.database:
        raise SystemExit("Noradi DB_NAME .env faila vai --database argumentu.")
    connection = connect(config)
    try:
        context = generate_context(connection, config.database, args.tables)
        write_text(args.output, context)
        print(f"Konteksts saglabats: {args.output}")
        return 0
    finally:
        connection.close()


def command_generate_sql(args: argparse.Namespace) -> int:
    gemini = read_gemini_config(args)
    context = read_text(args.context)
    prompt = f"""
Tu esi datu analitikas asistents. Izmanto tikai zemak doto MySQL datubazes strukturas kontekstu.
Uzraksti vienu MySQL SELECT vaicajumu, kas atgriez agregatus raditajus lietotaja vajadzibai.

Noteikumi:
- Atbilde satur tikai SQL kodu, bez paskaidrojumiem.
- Neizmanto INSERT, UPDATE, DELETE, DROP, ALTER, CREATE vai citas datu/strukturas mainisanas komandas.
- Neatlasi visas neapstradatas rindas; izmanto agregaciju, GROUP BY, COUNT, SUM, AVG, MIN vai MAX, ja tas atbilst uzdevumam.

Lietotaja vajadziba:
{args.question}

Datubazes konteksts:
{context}
""".strip()
    sql = extract_sql(call_gemini(gemini, prompt))
    validate_select_sql(sql)
    write_text(args.output, sql + "\n")
    print(f"SQL saglabats: {args.output}")
    return 0


def command_run_sql(args: argparse.Namespace) -> int:
    config = read_db_config(args)
    sql = read_text(args.sql)
    validate_select_sql(sql)

    connection = connect(config)
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql)
        rows = cursor.fetchmany(args.max_rows)
        payload = {
            "sql": sql,
            "max_rows": args.max_rows,
            "rows": [{key: json_safe(value) for key, value in row.items()} for row in rows],
        }
        write_text(args.output, json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"Agregatie rezultati saglabati: {args.output}")
        return 0
    finally:
        connection.close()


def command_describe(args: argparse.Namespace) -> int:
    gemini = read_gemini_config(args)
    context = read_text(args.context)
    sql = read_text(args.sql)
    result = read_text(args.result)
    prompt = f"""
Tu esi datu analitikas asistents. Apraksti agregatus SQL rezultatus latviesu valoda.
Balsties tikai uz datubazes strukturas kontekstu, SQL vaicajumu un agregato rezultatu JSON.

Atbilde:
- iss kopsavilkums;
- galvenie secinajumi;
- piesardzibas piezimes, ja metadati nepasaka biznesa nozimi.

Datubazes konteksts:
{context}

SQL:
{sql}

Agregatie rezultati JSON:
{result}
""".strip()
    description = call_gemini(gemini, prompt)
    write_text(args.output, description + "\n")
    print(f"Apraksts saglabats: {args.output}")
    return 0


def command_generate_plan(args: argparse.Namespace) -> int:
    gemini = read_gemini_config(args)
    context = read_text(args.context)
    plan = generate_plan_text(gemini, context, args.question, args.items)
    write_text(args.output, plan)
    print(f"Plans saglabats: {args.output}")
    return 0


def command_process_plan(args: argparse.Namespace) -> int:
    config = read_db_config(args)
    if not config.database:
        raise SystemExit("Noradi DB_NAME .env faila vai --database argumentu.")
    gemini = read_gemini_config(args)
    context = read_text(args.context)
    plan_text = read_text(args.plan)
    process_plan_to_report(
        db_config=config,
        gemini=gemini,
        context=context,
        plan_text=plan_text,
        question=args.question,
        output=args.output,
        max_rows=args.max_rows,
    )
    print(f"Atskaite saglabata: {args.output}")
    return 0


def command_report(args: argparse.Namespace) -> int:
    config = read_db_config(args)
    if not config.database:
        raise SystemExit("Noradi DB_NAME .env faila vai --database argumentu.")
    gemini = read_gemini_config(args)

    connection = connect(config)
    try:
        context = generate_context(connection, config.database, args.tables)
    finally:
        connection.close()

    write_text(args.context_output, context)
    print(f"Konteksts saglabats: {args.context_output}")

    plan = generate_plan_text(gemini, context, args.question, args.items)
    write_text(args.plan_output, plan)
    print(f"Plans saglabats: {args.plan_output}")

    process_plan_to_report(
        db_config=config,
        gemini=gemini,
        context=context,
        plan_text=plan,
        question=args.question,
        output=args.output,
        max_rows=args.max_rows,
    )
    print(f"Atskaite saglabata: {args.output}")
    return 0


def command_all(args: argparse.Namespace) -> int:
    config = read_db_config(args)
    if not config.database:
        raise SystemExit("Noradi DB_NAME .env faila vai --database argumentu.")

    connection = connect(config)
    try:
        context = generate_context(connection, config.database, args.tables)
        write_text(args.context_output, context)
        print(f"Konteksts saglabats: {args.context_output}")
    finally:
        connection.close()

    sql_args = argparse.Namespace(
        gemini_api_key=args.gemini_api_key,
        gemini_model=args.gemini_model,
        question=args.question,
        context=args.context_output,
        output=args.sql_output,
    )
    command_generate_sql(sql_args)

    run_args = argparse.Namespace(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        sql=args.sql_output,
        output=args.result_output,
        max_rows=args.max_rows,
    )
    command_run_sql(run_args)

    describe_args = argparse.Namespace(
        gemini_api_key=args.gemini_api_key,
        gemini_model=args.gemini_model,
        context=args.context_output,
        sql=args.sql_output,
        result=args.result_output,
        output=args.description_output,
    )
    command_describe(describe_args)
    print("Pilna plusma pabeigta.")
    return 0


def add_common_db_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="MySQL servera adrese.")
    parser.add_argument("--port", type=int, help="MySQL ports.")
    parser.add_argument("--user", help="MySQL lietotajs.")
    parser.add_argument("--password", help="MySQL parole.")
    parser.add_argument("--database", help="Datubazes nosaukums.")


def add_common_gemini_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--gemini-api-key", help="Gemini API atslega.")
    parser.add_argument("--gemini-model", help="Gemini modelis.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MySQL context and Gemini SQL workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-databases", help="Paradit pieejamas datubazes.")
    add_common_db_args(list_parser)
    list_parser.set_defaults(func=command_list_databases)

    context_parser = subparsers.add_parser("context", help="Izveidot datubazes strukturas kontekstu.")
    add_common_db_args(context_parser)
    context_parser.add_argument("--tables", nargs="+", help="Ieklaut tikai noraditas tabulas.")
    context_parser.add_argument("--output", default="output/context.md", help="Konteksta fails.")
    context_parser.set_defaults(func=command_context)

    sql_parser = subparsers.add_parser("generate-sql", help="Generet agregatu SQL ar Gemini.")
    add_common_gemini_args(sql_parser)
    sql_parser.add_argument("--question", required=True, help="Velamais agregatu raditajs.")
    sql_parser.add_argument("--context", default="output/context.md", help="Konteksta fails.")
    sql_parser.add_argument("--output", default="output/query.sql", help="SQL izvades fails.")
    sql_parser.set_defaults(func=command_generate_sql)

    run_parser = subparsers.add_parser("run-sql", help="Izpildit agregatu SELECT vaicajumu.")
    add_common_db_args(run_parser)
    run_parser.add_argument("--sql", default="output/query.sql", help="SQL fails.")
    run_parser.add_argument("--output", default="output/result.json", help="Rezultatu JSON fails.")
    run_parser.add_argument("--max-rows", type=int, default=200, help="Maksimalais rezultatu rindu skaits.")
    run_parser.set_defaults(func=command_run_sql)

    describe_parser = subparsers.add_parser("describe", help="Aprakstit agregatus rezultatus ar Gemini.")
    add_common_gemini_args(describe_parser)
    describe_parser.add_argument("--context", default="output/context.md", help="Konteksta fails.")
    describe_parser.add_argument("--sql", default="output/query.sql", help="SQL fails.")
    describe_parser.add_argument("--result", default="output/result.json", help="Rezultatu JSON fails.")
    describe_parser.add_argument("--output", default="output/description.md", help="Apraksta fails.")
    describe_parser.set_defaults(func=command_describe)

    plan_parser = subparsers.add_parser(
        "generate-plan",
        help="Generet analitikas planu ar atdalitiem plana punktiem.",
    )
    add_common_gemini_args(plan_parser)
    plan_parser.add_argument("--question", required=True, help="Analitikas merkis.")
    plan_parser.add_argument("--context", default="output/context.md", help="Konteksta fails.")
    plan_parser.add_argument("--output", default="output/plan.txt", help="Plana izvades fails.")
    plan_parser.add_argument("--items", type=int, default=5, help="Plana punktu skaits.")
    plan_parser.set_defaults(func=command_generate_plan)

    process_parser = subparsers.add_parser(
        "process-plan",
        help="Apstradat plana punktus, izveidot SQL, vizualus un HTML atskaiti.",
    )
    add_common_db_args(process_parser)
    add_common_gemini_args(process_parser)
    process_parser.add_argument("--question", required=True, help="Analitikas merkis atskaitei.")
    process_parser.add_argument("--context", default="output/context.md", help="Konteksta fails.")
    process_parser.add_argument("--plan", default="output/plan.txt", help="Plana fails.")
    process_parser.add_argument("--output", default="output/report.html", help="HTML atskaites fails.")
    process_parser.add_argument(
        "--max-rows",
        type=int,
        default=200,
        help="Maksimalais rezultatu rindu skaits katram plana punktam.",
    )
    process_parser.set_defaults(func=command_process_plan)

    report_parser = subparsers.add_parser(
        "report",
        help="Palaist pilnu plana, SQL, vizualizaciju un HTML atskaites plusmu.",
    )
    add_common_db_args(report_parser)
    add_common_gemini_args(report_parser)
    report_parser.add_argument("--question", required=True, help="Analitikas merkis.")
    report_parser.add_argument("--tables", nargs="+", help="Ieklaut tikai noraditas tabulas konteksta.")
    report_parser.add_argument("--items", type=int, default=5, help="Plana punktu skaits.")
    report_parser.add_argument(
        "--context-output",
        default="output/context.md",
        help="Konteksta izvades fails.",
    )
    report_parser.add_argument("--plan-output", default="output/plan.txt", help="Plana izvades fails.")
    report_parser.add_argument("--output", default="output/report.html", help="HTML atskaites fails.")
    report_parser.add_argument(
        "--max-rows",
        type=int,
        default=200,
        help="Maksimalais rezultatu rindu skaits katram plana punktam.",
    )
    report_parser.set_defaults(func=command_report)

    all_parser = subparsers.add_parser("all", help="Palaist visu plusmu ar vienu komandu.")
    add_common_db_args(all_parser)
    add_common_gemini_args(all_parser)
    all_parser.add_argument("--question", required=True, help="Velamais agregatu raditajs.")
    all_parser.add_argument("--tables", nargs="+", help="Ieklaut tikai noraditas tabulas konteksta.")
    all_parser.add_argument(
        "--context-output",
        default="output/context.md",
        help="Konteksta izvades fails.",
    )
    all_parser.add_argument("--sql-output", default="output/query.sql", help="SQL izvades fails.")
    all_parser.add_argument(
        "--result-output",
        default="output/result.json",
        help="Rezultatu JSON izvades fails.",
    )
    all_parser.add_argument(
        "--description-output",
        default="output/description.md",
        help="Apraksta izvades fails.",
    )
    all_parser.add_argument("--max-rows", type=int, default=200, help="Maksimalais rezultatu rindu skaits.")
    all_parser.set_defaults(func=command_all)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
