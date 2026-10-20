# Atualizar o serviço `robo-trade` que já está no Railway

O serviço atual é um **cron** (roda e encerra). A versão nova mantém esse formato: a cada 5 min ela olha o relógio de Brasília e decide o que fazer (`python main.py tick`).

## 1. Subir o código novo
No GitHub (repositório `robo-trade-v2`):
1. *Add file → Upload files*: arraste **todo o conteúdo** da pasta do zip (inclusive a pasta `tests`). Marque *Create a new branch* (`atualizacao-v3`) se quiser revisar antes; os arquivos de mesmo nome são substituídos.
2. Apague os 4 arquivos antigos que não existem mais: `brapi_client.py`, `telegram_client.py`, `health_check.py`, `test_telegram.py` (abra o arquivo → ícone de lixeira → *Commit*).
3. Mescle na `main` (se usou branch). O Railway faz o deploy sozinho.

## 2. No Railway, serviço `robo-trade`
1. **Settings → Cron Schedule:** troque para (vazio: sem cron) (UTC = 08:00–17:55 em Brasília, seg–sex).
2. **Settings → Deploy → Start Command:** `python main.py tick` (o `railway.json` já define; confira que não há outro comando manual).
3. **Volume (obrigatório):** clique com o botão direito no serviço → *Attach Volume* → mount path `/data`. Sem volume o robô se recusa a rodar (a execução aparece vermelha), porque o histórico de IV seria apagado a cada passada.
   Se o Railway não permitir volume num serviço cron, use o modo sempre ligado: remova o Cron Schedule e troque o Start Command para `python main.py serve`.
4. **Variables** (os nomes antigos continuam valendo):
   - `BRAPI_TOKEN` (ou `BRAPI_API_KEY`)
   - `TELEGRAM_BOT_TOKEN` (ou `TELEGRAM_TOKEN`) e `TELEGRAM_CHAT_ID`
   - `DRY_RUN=true` na primeira semana
   - Apague `TESTE_MODE` (não existe mais; a versão antiga usava opções simuladas).

## 3. Primeira execução
A cada passada o robô reconstrói até 4 min de histórico de IV (12 meses × 17 ativos) e retoma na seguinte. Enquanto o histórico não completa (algumas horas a 1–2 dias de passadas), nenhum alerta sai. Acompanhe em *Cron Runs → View logs*; o fim aparece como "EOD concluido".

Depois: `DRY_RUN=false`. Para testar o Telegram, troque o Start Command por `python main.py telegram-test` por uma execução e volte para `python main.py tick`.

## 4. Agenda (Brasília, seg–sex)
08:30 calcula IV e monta a lista a vigiar · 09:45–10:10 vigia a cada 5 min · 10:15–17:45 a cada 15 min.

## 5. Segurança
O arquivo antigo `test_telegram.py` tinha um token de bot escrito no código e o repositório é público. Se esse token for real, revogue no @BotFather (`/revoke`) e gere outro. O arquivo foi removido, mas continua no histórico do Git.
