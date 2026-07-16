import os
import re
import unicodedata
from pathlib import Path

import duckdb
import pandas as pd
import requests
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "cnsf.duckdb"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL = os.getenv("LOCAL_MODEL", "sqlcoder")

ENTITY_ALIASES = {
    "Chubb de México": "Chubb Seguros México",
    "Chubb de Mexico": "Chubb Seguros México",
}

st.set_page_config(
    page_title="Copiloto CNSF",
    page_icon="📊",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        header {visibility: hidden;}

        .block-container {
            max-width: 900px;
            padding-top: 2.5rem;
            padding-bottom: 6rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

if not DB_PATH.exists():
    st.error(f"No encuentro la base de datos en: {DB_PATH}")
    st.stop()


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", str(text))
        if unicodedata.category(c) != "Mn"
    )


def norm_text(text: str) -> str:
    return strip_accents(text).lower().strip()


def extract_year(text: str):
    match = re.search(r"\b(20\d{2})\b", text)
    return int(match.group(1)) if match else None


def normalize_question(question: str) -> str:
    q = question
    for original, replacement in ENTITY_ALIASES.items():
        q = q.replace(original, replacement)
    return q


def build_rule_based_sql(question: str):
    q = norm_text(question)
    year = extract_year(q)

    asks_entities = "entidades" in q or "entidad" in q
    asks_top = "mayor" in q or "top" in q
    asks_utility = "utilidad" in q or "perdida" in q
    asks_last_cut = "ultimo corte" in q or ("ultimo" in q and "corte" in q)
    asks_negative = "negativa" in q or "negativas" in q

    if asks_entities and asks_utility and asks_top and year:
        return f"""
        select
          entidad,
          round(sum(importe), 2) as utilidad_operacion
        from estado_resultados
        where anio_corte = {year}
          and descripcion = 'Utilidad (Pérdida) de la Operación'
        group by entidad
        order by utilidad_operacion desc
        limit 10
        """

    if asks_entities and asks_utility and asks_top and asks_last_cut:
        return """
        with ultimo as (
          select max(fecha_corte) as fecha_corte
          from estado_resultados
        )
        select
          entidad,
          fecha_corte,
          round(sum(importe), 2) as utilidad_operacion
        from estado_resultados
        where fecha_corte = (select fecha_corte from ultimo)
          and descripcion = 'Utilidad (Pérdida) de la Operación'
        group by entidad, fecha_corte
        order by utilidad_operacion desc
        limit 10
        """

    if asks_entities and asks_utility and asks_negative and asks_last_cut:
        return """
        with ultimo as (
          select max(fecha_corte) as fecha_corte
          from estado_resultados
        )
        select
          entidad,
          fecha_corte,
          round(sum(importe), 2) as utilidad_operacion
        from estado_resultados
        where fecha_corte = (select fecha_corte from ultimo)
          and descripcion = 'Utilidad (Pérdida) de la Operación'
        group by entidad, fecha_corte
        having sum(importe) < 0
        order by utilidad_operacion asc
        limit 50
        """

    return None


def normalize_aliases(sql: str) -> str:
    replacements = {
        "description": "descripcion",
        "operation": "operacion",
        "entity": "entidad",
        "amount": "importe",
        "breakdown": "desagregado",
    }
    out = sql
    for old, new in replacements.items():
        out = re.sub(rf"\b{old}\b", new, out, flags=re.IGNORECASE)
    return out


def preview(text: str, max_len: int = 1000) -> str:
    t = str(text or "").strip()
    return t if len(t) <= max_len else t[:max_len] + "\n...[truncado]"


def extract_sql_candidate(text: str):
    t = str(text or "").strip()
    t = re.sub(r"<think>[\s\S]*?</think>", " ", t, flags=re.IGNORECASE).strip()

    fenced = re.search(r"```(?:sql)?\s*([\s\S]*?)```", t, flags=re.IGNORECASE)
    if fenced:
        t = fenced.group(1).strip()

    match = re.search(r"\b(with|select)\b[\s\S]*", t, flags=re.IGNORECASE)
    if not match:
        return None

    sql = match.group(0).strip()
    semi = sql.find(";")
    if semi >= 0:
        sql = sql[: semi + 1]

    return sql.strip()


def sanitize_sql(raw_text: str) -> str:
    candidate = extract_sql_candidate(raw_text)

    if not candidate:
        raise RuntimeError(
            "El modelo no devolvió una consulta SELECT/WITH válida.\n\n"
            "Respuesta cruda del modelo:\n" + preview(raw_text)
        )

    q = normalize_aliases(candidate).strip()

    if not re.match(r"^(select|with)\b", q, flags=re.IGNORECASE):
        raise RuntimeError("El SQL extraído no empieza con SELECT o WITH.\n\nSQL:\n" + q)

    if re.search(
        r"\b(drop|delete|update|insert|alter|truncate|create|attach|copy)\b",
        q,
        flags=re.IGNORECASE,
    ):
        raise RuntimeError("La consulta contiene operaciones no permitidas.")

    is_aggregate = bool(
        re.search(r"\b(count|sum|avg|min|max)\s*\(", q, flags=re.IGNORECASE)
        or re.search(r"\bgroup\s+by\b", q, flags=re.IGNORECASE)
    )
    has_limit = bool(re.search(r"\blimit\b", q, flags=re.IGNORECASE))

    if not is_aggregate and not has_limit:
        q += " LIMIT 50"

    return q


def semantic_check(sql: str, user_question: str):
    q = str(sql or "")
    uq = norm_text(user_question)

    if re.search(
        r"\b(importe|desagregado|anio_corte|trimestre_corte)\b\s*(=|<>|!=|like|ilike)\s*'[^']*[A-Za-zÁÉÍÓÚáéíóúÑñ][^']*'",
        q,
        flags=re.IGNORECASE,
    ):
        return "Estás comparando una columna numérica con texto. Usa descripcion para conceptos financieros y operacion para ramos."

    if re.search(r"\b(utilidad|p[eé]rdida|primas?)\b", user_question, flags=re.IGNORECASE) and not re.search(
        r"\bdescripcion\b", q, flags=re.IGNORECASE
    ):
        return "Cuando el usuario habla de utilidad, pérdida o primas, normalmente debes filtrar por descripcion."

    if ("entidades" in uq or "entidad" in uq) and ("utilidad" in uq or "perdida" in uq):
        if not re.search(r"\bentidad\b", q, flags=re.IGNORECASE):
            return "Si el usuario pregunta por entidades, la consulta debe seleccionar o agrupar por entidad."

    if re.search(r"\bcompar[aá]|vs\b", user_question, flags=re.IGNORECASE) and not re.search(
        r"\bentidad\s+in\s*\(", q, flags=re.IGNORECASE
    ):
        return "Si el usuario compara entidades, usa entidad IN (...) y agrupa por entidad."

    return None


def ask_ollama(prompt: str) -> str:
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=180,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("response", "")
    except requests.RequestException as e:
        raise RuntimeError(
            "No pude conectarme a Ollama. Asegúrate de que esté corriendo en localhost:11434."
        ) from e


def build_main_prompt(user_question: str) -> str:
    return "\n".join(
        [
            "### Task",
            "Generate exactly one DuckDB SQL query that answers the user question.",
            "Return only SQL.",
            "Do not use markdown.",
            "Do not explain anything.",
            "The user question is in Spanish.",
            "",
            "### Database Schema",
            "CREATE TABLE estado_resultados (",
            "  fecha_corte_raw VARCHAR,",
            "  fecha_corte DATE,",
            "  anio_corte INTEGER,",
            "  trimestre_corte INTEGER,",
            "  entidad VARCHAR,",
            "  id_nivel VARCHAR,",
            "  descripcion VARCHAR,",
            "  operacion VARCHAR,",
            "  importe DOUBLE,",
            "  desagregado DOUBLE",
            ");",
            "",
            "### Important Rules",
            "- Use ONLY these exact column names.",
            "- Never use English column names like description, operation, entity, amount, breakdown.",
            '- descripcion = financial concept, for example "Utilidad (Pérdida) de la Operación" or "Primas de Retención Devengadas".',
            '- operacion = business line, for example "Daños", "Cascos", "Vida Grupo".',
            "- If the user asks about utilidad, pérdida, primas, or another financial concept, filter by descripcion, not operacion.",
            "- If the user compares two entities, use entidad IN (...) and group by entidad.",
            "- If the user asks for the latest reporting date, use max(fecha_corte) over the relevant subset.",
            "- For comparisons or totals, use sum(importe), unless the user explicitly asks for detailed rows.",
            "- Never compare importe or desagregado against text.",
            "",
            "### Example 1",
            "Question: ¿Cuál fue la utilidad de Chubb Seguros México en el último corte?",
            "SQL: WITH ultimo AS (SELECT max(fecha_corte) AS fecha_corte FROM estado_resultados WHERE entidad = 'Chubb Seguros México') SELECT entidad, fecha_corte, sum(importe) AS utilidad_operacion FROM estado_resultados WHERE entidad = 'Chubb Seguros México' AND descripcion = 'Utilidad (Pérdida) de la Operación' AND fecha_corte = (SELECT fecha_corte FROM ultimo) GROUP BY entidad, fecha_corte;",
            "",
            "### Example 2",
            "Question: Compárame AXA Seguros vs Chubb Seguros México en 2025 para Utilidad (Pérdida) de la Operación",
            "SQL: SELECT entidad, fecha_corte, sum(importe) AS utilidad_operacion FROM estado_resultados WHERE entidad IN ('AXA Seguros', 'Chubb Seguros México') AND anio_corte = 2025 AND descripcion = 'Utilidad (Pérdida) de la Operación' GROUP BY entidad, fecha_corte ORDER BY fecha_corte, entidad;",
            "",
            "### Example 3",
            "Question: ¿Cuáles son las 10 entidades con mayor utilidad en 2025?",
            "SQL: SELECT entidad, round(sum(importe), 2) AS utilidad_operacion FROM estado_resultados WHERE anio_corte = 2025 AND descripcion = 'Utilidad (Pérdida) de la Operación' GROUP BY entidad ORDER BY utilidad_operacion DESC LIMIT 10;",
            "",
            "### User Question",
            user_question,
            "",
            "### SQL",
        ]
    )


def build_repair_prompt(user_question: str, bad_sql: str, reason: str) -> str:
    return "\n".join(
        [
            "### Task",
            "Fix the DuckDB SQL query below.",
            "Return only SQL.",
            "Do not use markdown.",
            "Do not explain anything.",
            "The user question is in Spanish.",
            "",
            "### Database Schema",
            "CREATE TABLE estado_resultados (",
            "  fecha_corte_raw VARCHAR,",
            "  fecha_corte DATE,",
            "  anio_corte INTEGER,",
            "  trimestre_corte INTEGER,",
            "  entidad VARCHAR,",
            "  id_nivel VARCHAR,",
            "  descripcion VARCHAR,",
            "  operacion VARCHAR,",
            "  importe DOUBLE,",
            "  desagregado DOUBLE",
            ");",
            "",
            "### Rules",
            "- Use ONLY the exact column names above.",
            "- descripcion is the financial concept.",
            "- operacion is the business line.",
            "- importe and desagregado are numeric.",
            "- Never use English aliases like description or operation.",
            "",
            "### User Question",
            user_question,
            "",
            "### Bad SQL",
            bad_sql,
            "",
            "### Reason",
            str(reason or ""),
            "",
            "### Fixed SQL",
        ]
    )


def generate_sql(user_question: str, previous_sql: str = None, reason: str = None) -> str:
    prompt = build_repair_prompt(user_question, previous_sql, reason) if previous_sql else build_main_prompt(user_question)
    raw_sql = ask_ollama(prompt)
    return sanitize_sql(raw_sql)


def run_sql(sql: str) -> pd.DataFrame:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


def format_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].astype(str).str.slice(0, 10)
        elif pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].round(2)
    return out


def build_answer_text(df: pd.DataFrame) -> str:
    if df.empty:
        return "No encontré resultados para esa consulta."

    if len(df) == 1 and len(df.columns) == 1:
        col = df.columns[0]
        val = df.iloc[0, 0]
        return f"Encontré un resultado: **{col} = {val}**."

    if len(df) == 1:
        return "Encontré **1 fila**. Te la muestro abajo."

    return f"Encontré **{len(df):,} filas**. Te muestro una tabla con los resultados."


def answer_question(question: str):
    normalized_question = normalize_question(question)

    rule_sql = build_rule_based_sql(normalized_question)
    if rule_sql:
        df = format_df(run_sql(rule_sql))
        answer_text = build_answer_text(df)
        return normalized_question, rule_sql, df, answer_text

    sql = generate_sql(normalized_question)

    for attempt in range(3):
        issue = semantic_check(sql, normalized_question)

        if issue:
            if attempt == 2:
                raise RuntimeError(issue + "\nSQL problemático:\n" + sql)
            sql = generate_sql(normalized_question, sql, issue)
            continue

        try:
            df = format_df(run_sql(sql))
            answer_text = build_answer_text(df)
            return normalized_question, sql, df, answer_text
        except Exception as e:
            if attempt == 2:
                raise
            sql = generate_sql(normalized_question, sql, str(e))

    raise RuntimeError("No se pudo generar una consulta válida.")


def submit_suggested_question(text: str):
    st.session_state.pending_question = text


if "messages" not in st.session_state:
    st.session_state.messages = []

if "pending_question" not in st.session_state:
    st.session_state.pending_question = None


top_left, top_right = st.columns([8, 2])
with top_left:
    st.title("📊 Copiloto CNSF")
with top_right:
    if st.button("Nueva conversación", use_container_width=True):
        st.session_state.messages = []
        st.session_state.pending_question = None
        st.rerun()

if not st.session_state.messages:
    st.markdown("### Bienvenido")
    st.write("Pregúntame sobre la base de CNSF en lenguaje natural.")
    st.caption("Ejemplos sugeridos para la demo:")

    c1, c2 = st.columns(2)
    with c1:
        st.button(
            "¿Cuál fue la utilidad de Chubb Seguros México en el último corte?",
            use_container_width=True,
            on_click=submit_suggested_question,
            args=("¿Cuál fue la utilidad de Chubb Seguros México en el último corte?",),
        )
        st.button(
            "¿Cuáles son las 10 entidades con mayor utilidad en 2025?",
            use_container_width=True,
            on_click=submit_suggested_question,
            args=("¿Cuáles son las 10 entidades con mayor utilidad en 2025?",),
        )

    with c2:
        st.button(
            "Compárame AXA Seguros vs Chubb de México en 2025 para Utilidad (Pérdida) de la Operación",
            use_container_width=True,
            on_click=submit_suggested_question,
            args=("Compárame AXA Seguros vs Chubb de México en 2025 para Utilidad (Pérdida) de la Operación",),
        )
        st.button(
            "Muéstrame las entidades con utilidad negativa en el último corte",
            use_container_width=True,
            on_click=submit_suggested_question,
            args=("Muéstrame las entidades con utilidad negativa en el último corte",),
        )

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["text"])

        if message["role"] == "assistant":
            if message.get("normalized_question") and message["normalized_question"] != message.get("original_question"):
                st.caption("Interpreté la consulta como: " + message["normalized_question"])

            if message.get("sql"):
                with st.expander("Ver SQL generado"):
                    st.code(message["sql"], language="sql")

            if message.get("data") is not None:
                st.dataframe(message["data"], use_container_width=True)

question = st.chat_input("Escribe tu pregunta aquí...")

if st.session_state.pending_question:
    question = st.session_state.pending_question
    st.session_state.pending_question = None

if question:
    st.session_state.messages.append(
        {
            "role": "user",
            "text": question,
        }
    )

    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("Consultando la base..."):
            try:
                normalized_question, sql, df, answer_text = answer_question(question)

                st.write(answer_text)

                if normalized_question != question:
                    st.caption("Interpreté la consulta como: " + normalized_question)

                with st.expander("Ver SQL generado"):
                    st.code(sql, language="sql")

                if not df.empty:
                    st.dataframe(df, use_container_width=True)

                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "text": answer_text,
                        "original_question": question,
                        "normalized_question": normalized_question,
                        "sql": sql,
                        "data": df,
                    }
                )

            except Exception as e:
                error_text = str(e)
                st.error(error_text)
                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "text": "La consulta falló.",
                    }
                )
