import os

# Configurações do robô de opções

# Tokens e credenciais
BRAPI_TOKEN = os.getenv('BRAPI_TOKEN', '')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Universo de ativos
ATIVOS = ['PETR4', 'VALE3', 'BOVA11', 'ITUB4', 'BBDC4', 'BBAS3', 'B3SA3', 'WEGE3', 'MGLU3', 'LREN3', 'ITSA4', 'CIEL3', 'EMBR3', 'HAPV3', 'RADL3', 'TAEE11', 'CPFE3', 'EQTL3', 'SBSP3', 'VIVT3', 'GOAU4', 'USIM5']
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
