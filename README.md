# Darba virsma konteksta sagatavosanai

Python CLI riks MySQL servera strukturas konteksta izveidei un Gemini API izmantosanai SQL vaicajumu un agregatu aprakstu generesanai.

Riks nenolasa pilnus tabulu datus konteksta izveidei. Tas izmanto tikai metadatus: tabulu nosaukumus, kolonnas, datu tipus un ierobezojumus. Datu rindas tiek iegutas tikai tad, ja lietotajs palaiž agregatu SQL vaicajumu.

## Uzstadisana

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Faila `.env` ievadi:

- `DB_PASSWORD` - MySQL parole.
- `DB_NAME` - konkreta datubaze, ar kuru stradat.
- `GEMINI_API_KEY` - Google AI Studio / Gemini API atslega.

`.env` ir ieklauts `.gitignore`, lai noslepumi nenonaktu GitHub.

## Gemini API atslega

1. Atver Google AI Studio: https://aistudio.google.com/app/apikey
2. Izveido vai izvelies Google projektu.
3. Izveido API atslegu.
4. Ievieto to `.env` faila ka `GEMINI_API_KEY`.

Gemini REST API izmanto `generateContent` endpointu un `x-goog-api-key` autentifikacijas headeri, ka noradits Google AI for Developers dokumentacija: https://ai.google.dev/api

## Lietosana

Pieejamas datubazes:

```bash
python context_workspace.py list-databases
```

Konteksta izveide:

```bash
python context_workspace.py context --output output/context.md
```

SQL generesana velamajiem agregatiem:

```bash
python context_workspace.py generate-sql \
  --question "Paradi klientu skaitu pa statusiem" \
  --context output/context.md \
  --output output/query.sql
```

Agregata SQL izpilde:

```bash
python context_workspace.py run-sql --sql output/query.sql --output output/result.json
```

Agregato rezultatu apraksts ar Gemini:

```bash
python context_workspace.py describe \
  --context output/context.md \
  --sql output/query.sql \
  --result output/result.json \
  --output output/description.md
```

Pilna plusma:

```bash
python context_workspace.py context --output output/context.md
python context_workspace.py generate-sql --question "Velamie agregatie raditaji" --context output/context.md --output output/query.sql
python context_workspace.py run-sql --sql output/query.sql --output output/result.json
python context_workspace.py describe --context output/context.md --sql output/query.sql --result output/result.json --output output/description.md
```
