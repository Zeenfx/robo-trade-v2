"""
Health-check para validar que o novo código do robô está em execução.

Uso:
    python health_check.py

Saída esperada (novo código):
    [OK] NOVO CÓDIGO ATIVO
    - screener.obter_candidatos_com_detalhes: disponível
    - screener.AtivoInfo: disponível
    - main.executar_rodada: disponível

Saída em caso de erro:
    [ERRO] Código antigo ou inconsistente detectado.
    Detalhes: <exceção>
"""

import sys

def main() -> int:
    try:
        from screener import obter_candidatos_com_detalhes, AtivoInfo
        from main import executar_rodada

        print("[OK] NOVO CÓDIGO ATIVO")
        print("- screener.obter_candidatos_com_detalhes: disponível")
        print("- screener.AtivoInfo: disponível")
        print("- main.executar_rodada: disponível")
        return 0

    except Exception as e:
        print("[ERRO] Código antigo ou inconsistente detectado.")
        print(f"Detalhes: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
