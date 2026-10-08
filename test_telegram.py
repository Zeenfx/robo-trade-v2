import httpx
import asyncio

TELEGRAM_TOKEN = "7518015689:AAH8BfzKj8vN9G5L2qR4mT6sU8wX0yZ1aB3"
CHAT_ID = "6079803525"

async def send_test():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    msg = """🚨 GATILHO DE OPÇÃO - TESTE

📈 Ativo: PETR4 (TESTE)
🔹 Tipo: CALL
🔹 Opção: PETRC5991
🔹 Strike: R$ 59,92
🔹 Preço: R$ 2,15
🔹 Delta: 0,33
🔹 IV: 42,5%

💰 Preço do Ativo: R$ 54,47
📊 Moneyness: OTM (+10,0%)

⚙️ Setup: Delta ~0.3 (alvo: 0.3)

🔗 <a href="https://profit.net.br/chart/PETR4">Abrir no Profit</a>

✅ Se recebeu esta mensagem, o Telegram está configurado corretamente!
"""
    
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            print(f"✅ Mensagem enviada! Status: {resp.status_code}")
            print(f"Response: {resp.json()}")
        except Exception as e:
            print(f"❌ Erro: {e}")

if __name__ == '__main__':
    asyncio.run(send_test())
