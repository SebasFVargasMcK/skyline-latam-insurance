import OpenAI from 'openai';

const client = new OpenAI();

const response = await client.responses.create({
  model: 'gpt-5.4-mini',
  input: 'Respóndeme solo con: API OK'
});

console.log(response.output_text);
