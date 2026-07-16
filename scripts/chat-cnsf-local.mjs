import OpenAI from 'openai';
import { DuckDBInstance } from '@duckdb/node-api';

const question = process.argv.slice(2).join(' ').trim();
if (!question) {
  console.error('Uso: node .\\scripts\\chat-cnsf-local.mjs "tu pregunta"');
  process.exit(1);
}

const model = process.env.LOCAL_MODEL || 'llama3.2';

const client = new OpenAI({
  baseURL: 'http://localhost:11434/v1',
  apiKey: 'ollama', // requerido por el cliente, pero Ollama lo ignora
});

function sanitizeSql(sql) {
  let q = String(sql || '').trim();

  q = q.replace(/^```sql/i, '').replace(/^```/, '').replace(/```$/i, '').trim();

  if (!/^(select|with)\b/i.test(q)) {
    throw new Error('El modelo no devolvió una consulta SELECT/WITH válida.');
  }

  if (/\b(drop|delete|update|insert|alter|truncate|create|attach|copy)\b/i.test(q)) {
    throw new Error('La consulta contiene operaciones no permitidas.');
  }

  const isAggregate =
    /\b(count|sum|avg|min|max)\s*\(/i.test(q) ||
    /\bgroup\s+by\b/i.test(q);

  const hasLimit = /\blimit\b/i.test(q);

  if (!isAggregate && !hasLimit) {
    q += ' LIMIT 50';
  }

  return q;
}

const schema = `
Tabla: estado_resultados

Columnas:
- fecha_corte_raw (texto)
- fecha_corte (fecha)
- anio_corte (entero)
- trimestre_corte (entero)
- entidad (texto)
- id_nivel (texto)
- descripcion (texto)
- operacion (texto)
- importe (numero)
- desagregado (numero)

Reglas de negocio:
- Si el usuario pide "último corte", usa max(fecha_corte).
- Si pide comparar entidades, agrupa por entidad y/o fecha.
- Si pide utilidad, normalmente la descripcion relevante es "Utilidad (Pérdida) de la Operación".
- Devuelve SOLO SQL para DuckDB.
- Usa solo SELECT o WITH.
- No uses markdown.
`;

const sqlResponse = await client.chat.completions.create({
  model,
  temperature: 0,
  messages: [
    {
      role: 'system',
      content:
        'Eres un traductor de lenguaje natural a SQL para DuckDB. ' +
        'Devuelve solo SQL válido y nada más.'
    },
    {
      role: 'user',
      content: `${schema}\nPregunta: ${question}`
    }
  ]
});

const rawSql = sqlResponse.choices?.[0]?.message?.content ?? '';
const sql = sanitizeSql(rawSql);

const db = await DuckDBInstance.create('data/cnsf.duckdb');
const connection = await db.connect();

try {
  const reader = await connection.runAndReadAll(sql);
  const rows = reader.getRowObjectsJson();

  console.log('\nSQL generado:\n');
  console.log(sql);

  console.log('\nResultados:\n');
  if (!rows.length) {
    console.log('Sin resultados.');
  } else {
    console.table(rows.slice(0, 20));
    if (rows.length > 20) {
      console.log(`Mostrando 20 de ${rows.length} filas.`);
    }
  }
} finally {
  connection.closeSync();
}