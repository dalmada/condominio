# Luís Gustavo Residence · Rateio residencial

Aplicação em português para um administrador lançar contas do prédio, calcular as parcelas de quatro apartamentos e acompanhar os pagamentos. Usa Django 5.2, SQLite e impressão pelo navegador.

## Abrir no computador

Requer Python 3.12 ou posterior. Execute na pasta do projeto.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Abra **http://127.0.0.1:8000/** e defina o usuário e a senha no primeiro acesso. Essa tela funciona no próprio computador com o banco ainda sem usuários. Para recuperar o acesso, execute `.venv/bin/python manage.py changepassword NOME_DO_USUARIO`.

## Interface React e Tailwind

Os cards das unidades e o gráfico da visão geral usam React e Tailwind. Para gerar os arquivos estáticos, execute na raiz do projeto.

```sh
npm --prefix frontend ci
npm --prefix frontend run build
```

## PostgreSQL no Supabase

O ambiente local começa em SQLite. Para conectar ao Postgres, configure `RATEIO_DATABASE_URL` no ambiente do servidor com a URL de conexão PostgreSQL do Supabase. A conexão usa TLS e o schema privado `patio`, que deve existir antes de executar as migrações do Django. Use a conexão direta ou o pooler em modo sessão. Guarde a URL como variável sensível no ambiente de hospedagem.

## Primeiro ciclo mensal

1. Em **Unidades**, informe o nome de exibição, o responsável e a ocupação dos apartamentos. Em **Categorias**, configure a divisão acordada e os participantes. Ocupação e participação são independentes.
2. Abra uma competência e escolha o vencimento dos apartamentos. Lance cada conta ou serviço com o vencimento próprio. Água e gás aceitam leituras em m³ e parcela fixa quando aplicável.
3. Confira o rateio, resolva as pendências e feche o mês. No primeiro uso, reproduza uma competência antiga e compare cada apartamento com seu controle.
4. Abra o demonstrativo de cada unidade e use **Imprimir ou salvar PDF** para compartilhá-lo. Em **Cobrança**, registre os pagamentos. Um estorno exige motivo e preserva o movimento original.
5. Para corrigir valores já publicados, abra uma retificação. Compare as diferenças e publique a nova versão. O histórico conserva os documentos anteriores e os pagamentos continuam vinculados à unidade e à competência.

As unidades iniciais têm chaves 101, 201, 301 e 401. Os nomes de exibição são editáveis. Água e Gás usam exclusivamente consumo medido. Energia e Faxina começam sem regra definida. O exemplo fictício de setembro integra os testes e não preenche o banco de trabalho.

## Testes e operação

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py test ledger
.venv/bin/python -m pip install -r ops/requirements.txt
.venv/bin/python -m unittest discover -s ops -p 'test_*.py'
```

Por padrão, `var/` mantém o banco, a chave local e os anexos privados. Para usar outro diretório, defina `RATEIO_DATA_DIR`. O acesso pelo celular fora deste computador depende de uma instalação com HTTPS. O [guia de operação](OPERACAO.md) descreve a publicação, a cópia diária criptografada para um volume remoto e a restauração.
