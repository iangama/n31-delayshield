N31 DelayShield

Sistema de mitigação de risco de atraso logístico em tempo real.

O DelayShield simula decisões operacionais com orçamento de risco, recalculo assíncrono e observabilidade ativa.
O objetivo é evitar atrasos antes que eles aconteçam.

O QUE O SISTEMA FAZ

O usuário define uma rota (waypoints no mapa).

Define um deadline.

O sistema calcula:

Distância

Duração estimada

ETA

Buffer disponível

Risco de atraso (%)

Um worker assíncrono recalcula risco dinamicamente.

A UI exibe status da rota:

Verde → Dentro do prazo

Amarelo → Risco moderado

Vermelho → Alto risco de atraso

O sistema não apenas calcula rota.
Ele decide sob incerteza.

FLUXO DO PRODUTO

Clique no mapa
Defina origem e destino
Escolha deadline
Criar viagem
Worker recalcula risco
UI atualiza com status colorido
Recalcular sob demanda se necessário

ARQUITETURA

API: FastAPI (Python)
Worker assíncrono: Celery + Redis
Banco: PostgreSQL 16
Proxy reverso: Traefik SAFE (:8880 / :8443)
Observabilidade:

Prometheus

Grafana

Loki

Promtail
UI: React + Vite + MapLibre

Tudo containerizado com Docker Compose.

Arquitetura distribuída real, não CRUD isolado.

COMO RODAR LOCALMENTE (WSL RECOMENDADO)

Clonar o repositório

git clone https://github.com/iangama/n31-delayshield.git

cd n31-delayshield

Subir containers

docker compose up -d --build

Acessar

UI → http://localhost:8880

Traefik dashboard → http://localhost:8081

Prometheus → http://localhost:8880/prometheus/

Grafana → http://localhost:8880/grafana/

PROVA DE API (SMOKE TEST)

Criar viagem:

BASE="http://localhost:8880
"

curl -X POST "$BASE/api/trips"
-H "Content-Type: application/json"
-d '{
"deadline_at":"2026-12-31T23:59:00Z",
"waypoints":[
{"lat":-19.9191,"lon":-43.9386},
{"lat":-23.5505,"lon":-46.6333}
]
}'

Listar viagens:

curl "$BASE/api/trips"

Recalcular risco:

curl -X POST "$BASE/api/trips/<trip_id>/recalc"

Preview de rota:

curl -X POST "$BASE/api/route/preview"
-H "Content-Type: application/json"
-d '{
"waypoints":[
{"lat":-19.9191,"lon":-43.9386},
{"lat":-23.5505,"lon":-46.6333}
]
}'

LÓGICA DE DECISÃO

O risco é derivado de:

ETA estimado

Deadline definido

Buffer disponível (minutos)

Distância e duração da rota

Buffer negativo aumenta risco.
Buffer positivo reduz risco.

Sistema simula tomada de decisão sob deadline real.

OBSERVABILIDADE

O projeto possui:

Métricas expostas em /metrics

Scraping via Prometheus

Logs centralizados via Loki

Dashboards em Grafana

Não é apenas backend funcional.
É sistema monitorável.

ESTRUTURA DO PROJETO

infra/ → configs de observabilidade e banco
services/
api/ → FastAPI
worker/ → Celery worker
ui/ → React + MapLibre
smoke/ → scripts de teste
docker-compose.yml

POR QUE ESTE PROJETO EXISTE

Simular responsabilidade operacional.

DelayShield representa:

Decisão sob incerteza

Controle de risco

Orçamento temporal

Reprocessamento assíncrono

Sistema observável

Ele não é um CRUD.
É um motor simplificado de decisão logística.

Autor:
Ian Gama<img width="1890" height="914" alt="image" src="https://github.com/user-attachments/assets/6abec293-84ed-4693-945d-c0bc434ed2c5" /> <img width="1894" height="912" alt="image" src="https://github.com/user-attachments/assets/678965e2-22e4-4885-b834-534d7fec720e" />

