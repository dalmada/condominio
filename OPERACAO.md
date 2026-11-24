# Operação do Rateio

## Publicação em um servidor Linux

Use uma única instância com disco persistente local para o SQLite. Os caminhos de exemplo supõem o projeto em `/srv/rateio`, uma conta de serviço `rateio` e um nome DNS próprio. Providencie `/var/lib/rateio` com proprietário `rateio` e modo `0700`. Reserve espaço em disco para o banco, anexos e um arquivo compactado temporário durante a cópia.

Na pasta do projeto, crie o ambiente e instale as dependências da aplicação e da cópia de segurança.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r ops/requirements.txt
```

Configure `/etc/rateio/web.env` com acesso restrito à conta de serviço. Substitua o domínio pelo endereço real e gere o segredo uma vez com `python3 -c 'import secrets; print(secrets.token_urlsafe(64))'`. Guarde uma cópia desse segredo no cofre de senhas para recuperar o serviço.

```ini
RATEIO_PRODUCTION=1
RATEIO_ALLOWED_HOSTS=rateio.seu-dominio.com.br
RATEIO_TRUST_PROXY=1
RATEIO_DATA_DIR=/var/lib/rateio
RATEIO_SECRET_KEY=SUBSTITUA_PELO_SEGREDO_GERADO
```

Execute estes comandos como a conta de serviço, com o arquivo de ambiente carregado. `createsuperuser` cria o administrador inicial e solicita sua senha no terminal.

```sh
set -a
. /etc/rateio/web.env
set +a
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py check --deploy
```

Em uma unidade systemd, use `User=rateio`, `WorkingDirectory=/srv/rateio`, `EnvironmentFile=/etc/rateio/web.env`, `UMask=0077`, `Restart=on-failure` e o seguinte comando no campo `ExecStart`.

```sh
/srv/rateio/.venv/bin/gunicorn --workers 1 --bind 127.0.0.1:8000 rateio.wsgi:application
```

Aponte o DNS para o servidor, libere HTTP/HTTPS para o proxy e configure o Caddy com o mesmo domínio. O Caddy termina o TLS e define o protocolo original para o Django. Somente o proxy deve alcançar o Gunicorn. O aplicativo serve os arquivos estáticos e exige sessão administrativa para abrir um anexo. A raiz privada permanece fora dos caminhos públicos do proxy.

```caddyfile
rateio.seu-dominio.com.br {
    reverse_proxy 127.0.0.1:8000
}
```

Após publicar, confira o redirecionamento HTTP para HTTPS, entre com o administrador, abra o demonstrativo de uma unidade e verifique que uma janela anônima exige login para abrir o mesmo endereço. Antes de uma atualização, faça e confira a cópia descrita a seguir. Pare o processo de aplicação para migrar, execute `migrate`, `collectstatic` e `check --deploy`, depois inicie o processo na nova versão.

## Cópia diária fora do servidor

Monte um volume de armazenamento hospedado em **outra máquina** em `/mnt/backup-rateio`. O operador deve confirmar o servidor e a exportação com `findmnt --mountpoint /mnt/backup-rateio`. A ferramenta verifica que o caminho é um ponto de montagem separado e recusa pastas locais comuns. A localização física do volume depende da configuração do sistema. Use um volume com suporte a renomeação atômica, permissões restritas e escrita pela conta que executa a cópia.

Gere a chave AES de 256 bits uma vez. O caminho de destino precisa ser novo. Guarde uma segunda via da chave em um cofre separado da aplicação e do volume de cópias. A perda da chave impede a recuperação. Preserve as chaves anteriores enquanto existirem cópias feitas com elas.

```sh
/srv/rateio/.venv/bin/python /srv/rateio/ops/backup.py chave /etc/rateio/backup.key
/srv/rateio/.venv/bin/python /srv/rateio/ops/backup.py salvar --dados /var/lib/rateio --montagem /mnt/backup-rateio --chave /etc/rateio/backup.key
```

Agende o segundo comando na crontab de uma conta com acesso ao banco, à chave e ao volume. O exemplo usa 02h15 no fuso do servidor. Configure o servidor em `America/Sao_Paulo` para executar nesse horário local.

```cron
15 2 * * * /srv/rateio/.venv/bin/python /srv/rateio/ops/backup.py salvar --dados /var/lib/rateio --montagem /mnt/backup-rateio --chave /etc/rateio/backup.key
```

O utilitário copia o SQLite pela API de backup e inclui os anexos de `private/`. Enquanto captura esses dados, uma transação impede gravações concorrentes no banco. A cópia temporária fica em diretório exclusivo da conta de serviço. Em hosts que exigem criptografia de disco, aponte `TMPDIR` para armazenamento local criptografado. O processo valida o banco, as chaves estrangeiras e os arquivos referenciados antes de publicar a cópia autenticada com AES-GCM. O volume remoto recebe apenas o arquivo cifrado.

O nome `rateio/rateio-AAAA-MM-DD.rbackup` usa a data de São Paulo. Uma execução bem-sucedida no mesmo dia atualiza a cópia daquele dia. Depois da publicação, são eliminadas cópias dessa aplicação anteriores à janela de 30 datas, contando hoje. Falhas preservam a última cópia válida. Configure o monitoramento do agendador para alertar sobre saída diferente de zero ou ausência do arquivo diário. A frequência diária pode deixar até um dia de lançamentos para reconciliar após uma perda, ou mais se a tarefa falhar.

## Ensaio de restauração

Restaure em um caminho **novo**, com diretório pai privado e espaço para o conteúdo compactado e descompactado. Substitua a data do exemplo pela de uma cópia existente. O comando autentica o arquivo antes de descompactá-lo, verifica o banco e os anexos referenciados, depois publica o diretório restaurado. O caminho ativo, caminhos dentro dele e destinos já existentes são recusados.

```sh
/srv/rateio/.venv/bin/python /srv/rateio/ops/backup.py restaurar /mnt/backup-rateio/rateio/rateio-2026-11-24.rbackup --chave /etc/rateio/backup.key --dados /var/lib/rateio --destino /var/lib/rateio-ensaio-20261124
```

Antes da entrada em produção, faça um ensaio com uma competência de teste fechada, seus quatro demonstrativos e pelo menos um anexo. Repita o ensaio trimestralmente com uma cópia recente da operação. Use a versão da aplicação que gerou a cópia. Abra o sistema com o banco restaurado em endereço de loopback em uma máquina de teste confiável, com o segredo da aplicação disponível. O comando usa o modo local apenas durante a conferência.

```sh
RATEIO_DATA_DIR=/var/lib/rateio-ensaio-20261124 RATEIO_PRODUCTION=0 RATEIO_TRUST_PROXY=0 RATEIO_ALLOWED_HOSTS=127.0.0.1 /srv/rateio/.venv/bin/python /srv/rateio/manage.py runserver 127.0.0.1:8001
```

Entre com o administrador e selecione a competência de referência. Reemita os quatro demonstrativos. Confira o total cobrado, os pagamentos e o saldo de cada unidade contra a referência anterior à cópia, incluindo pelo menos um pagamento parcial no ensaio inicial. Abra um anexo conhecido e compare seu conteúdo. Registre a data da cópia, a competência, os quatro saldos conferidos, o anexo testado e o resultado do ensaio. Encerre o servidor de teste e descarte a cópia de ensaio conforme a rotina de acesso aos documentos privados.

Para recuperação definitiva, primeiro suspenda o serviço e o agendamento, preserve o diretório original, valide uma restauração separada e reconcilie os lançamentos posteriores à cópia. Aponte `RATEIO_DATA_DIR` e o argumento `--dados` do agendamento para a restauração aprovada, com as permissões da conta de serviço. Refaça `collectstatic`, aplique migrações apenas se a versão do código exigir, inicie o serviço e gere uma nova cópia após a conferência.

Os testes locais da operação usam dados descartáveis e rodam pelo comando abaixo.

```sh
.venv/bin/python -m unittest discover -s ops -p 'test_*.py' -v
```

## Importação do SQLite no Supabase

Crie o schema privado `patio` no projeto de destino. Configure `RATEIO_DATABASE_URL` no ambiente local com uma conexão direta ou de pooler em modo sessão. Digite a credencial apenas em um terminal local confiável ou no gerenciador de variáveis da hospedagem.

Com uma cópia consistente do SQLite em mãos, aplique o schema do Django e execute o importador.

```sh
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py import_patio_sqlite /caminho/da/copia/db.sqlite3
```

O importador recusa um destino com dados de uso e confere as contagens e o valor total das despesas antes de confirmar a transação. Ele copia os registros e as contas do Django. Se houver anexos, copie os arquivos separadamente para o armazenamento privado usado em produção. Mantenha o SQLite original até conferir os saldos no novo ambiente.
