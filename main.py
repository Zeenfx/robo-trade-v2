import os, logging, asyncio, httpx
from brapi_client import get_quote, get_option_chain

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
TESTE = os.getenv("TESTE_MODE", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger()

async def send(msg):
    if not TOKEN or not CHAT:
        logger.error("Telegram sem token/chat")
        return
    async with httpx.AsyncClient() as c:
        await c.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT, "text": msg})

def main():
    logger.info("START")
    for sym in ["PETR4", "VALE3", "ITUB4"]:
        q = get_quote(sym)
        if not q.price: continue
        s = "CALL" if (sum(map(ord, sym)) % 2 == 0) else "PUT"
        ch = get_option_chain(sym)
        opts = ch.calls if s == "CALL" else ch.puts
        if not opts: continue
        o = min([x for x in opts if float(x.get("price") or 0) > 0], key=lambda x: abs(abs(float(x.get("delta") or 0)) - 0.55), default=None)
        if not o: continue
        msg = f"TESTE {s} {sym}\nOp: {o['symbol']}\nStrike: {o['strike']}\nPremio: {o['price']}\nLink: https://profit.net.br/chart/{sym}"
        asyncio.run(send(msg))
        logger.info(f"Sent {sym}")
    logger.info("END")

if __name__ == "__main__":
    main()
