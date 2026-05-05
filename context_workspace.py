#!/usr/bin/env python3
"""Generate MySQL metadata context and use Gemini for aggregate SQL workflows."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


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
    response = requests.post(
        url,
        headers={"x-goog-api-key": config.api_key, "Content-Type": "application/json"},
        json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
        timeout=60,
    )
    response.raise_for_status()
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

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
