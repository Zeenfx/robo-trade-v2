import os

# Configurações do robô de opções

# Tokens e credenciais
BRAPI_TOKEN = os.getenv('BRAPI_TOKEN', '')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Universo de ativos (apenas ativos com acesso gratuito irrestrito na Brapi)
ATIVOS = ['PETR4', 'VALE3', 'ITUB4', 'MGLU3']
UNIVERSE = ATIVOS

# Parâmetros de opção
DELTA_ALVO = 0.3
EXPIRACAO_DIAS = 30

# Tamanho de posição
LOTE = 100
LOTE_OPCOES = 1

# Risk management
STOP_LOSS_PCT = 0.3
ALVO_PCT = 0.5

# Horário de operação
HORARIO_INICIO = '10:00'
HORARIO_FIM = '17:00'
INTERVALO_MIN = 5

# Modo de operação
MODO = 'opcoes'
TESTE_MODE = False
