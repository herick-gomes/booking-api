# Rodando no Windows

O caminho mais simples é usar Docker Desktop. Você não precisa configurar PostgreSQL e Redis manualmente.

## Com Docker

Abra o Docker Desktop e aguarde iniciar. No PowerShell, entre na pasta onde guarda seus projetos:

```powershell
git clone https://github.com/herick-gomes/booking-api.git
cd booking-api
docker compose up --build -d
```

Abra `http://localhost:8000/ready`. Quando aparecer `ready`, abra `http://localhost:8000/docs`.

Execute a demonstração:

```powershell
docker compose exec api python scripts/demo.py
docker compose exec api python -m scripts.check_delivery
```

Ela cria dados fictícios, faz uma reserva, tenta reservar o mesmo horário, entra na fila de espera e cancela a primeira reserva. O sistema deve promover automaticamente quem estava esperando. A segunda execução verifica a confirmação entregue pelos processos de segundo plano. Os lembretes ficam dentro da API; nenhum e-mail ou WhatsApp é enviado.

Para acompanhar os processos e parar:

```powershell
docker compose logs -f api worker dispatcher
# Ctrl+C sai dos logs, sem desligar a aplicação.
docker compose down
```

Os dados ficam preservados nos volumes. Não use `docker compose down -v` se quiser mantê-los: a opção `-v` remove os volumes locais e os dados.

## Usando o Swagger manualmente

1. Crie uma conta em `POST /auth/register` com usuário em minúsculas e senha com pelo menos 12 caracteres.
2. Entre em `POST /auth/login` e copie o `access_token`.
3. Clique em **Authorize**, cole o token e confirme.
4. Crie um negócio, um profissional e um serviço. Use os IDs retornados.
5. Configure os horários em `/professionals/{id}/hours`: segunda é `0`, domingo é `6`; `480` significa 08:00.
6. Consulte a disponibilidade e crie a reserva usando um horário futuro com fuso, por exemplo `-03:00`.

Os exemplos de datas da documentação precisam ser ajustados para uma data disponível nos próximos 90 dias.

## Desenvolvimento sem Docker

Dentro da raiz `booking-api`, usando Python 3.12:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Configure um PostgreSQL e um Redis acessíveis nas URLs do `.env`. O Compose padrão não publica as portas desses bancos no computador; ele conecta os serviços internamente. Para desenvolvimento fora dos contêineres, use instâncias próprias ou uma configuração local de portas.

```powershell
alembic upgrade head
uvicorn app.main:app --reload --no-proxy-headers
```

O worker RQ com fork é destinado a Linux: use Docker ou WSL para worker e dispatcher. O Compose executa ambos no ambiente adequado.

Para testes, crie antes um banco vazio chamado `booking_test` e reserve um Redis para testes. Os testes apagam tabelas desse banco e limpam o banco Redis selecionado:

```powershell
$env:ENVIRONMENT = "test"
$env:DATABASE_URL = "postgresql+psycopg://booking:local-only@localhost:5432/booking_test"
$env:REDIS_URL = "redis://localhost:6379/15"
alembic upgrade head
pytest --cov --cov-fail-under=90
ruff check .
ruff format --check .
```

Essas variáveis valem para a sessão atual. Abra outro terminal para voltar à configuração normal do `.env`. O CI executa a suíte completa em Linux, inclusive os testes de worker.
