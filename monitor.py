"""
Monitor de preço da Amazon Brasil com alerta no Telegram.

Roda uma vez por execução (o GitHub Actions chama ele a cada ~15 min).
Configuração via variáveis de ambiente (no GitHub ficam em "Secrets"):
  PRODUCT_URL       link do produto na amazon.com.br
  MAX_PRICE         preço máximo pra te avisar (ex: 420)
  TELEGRAM_TOKEN    token do seu bot (pego no @BotFather)
  TELEGRAM_CHAT_ID  seu id de chat no Telegram
  TEST_ALERT        se for "true", só manda uma mensagem de teste
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

STATE_FILE = "state.json"
BRT = timezone(timedelta(hours=-3))
BLOCK_WARNING_AFTER = 8  # ~2h seguidas bloqueado -> te avisa uma vez

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

PRICE_SELECTORS = [
    "#corePrice_feature_div .a-price .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#price_inside_buybox",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
]


def now():
    return datetime.now(BRT).strftime("%d/%m %H:%M")


def env(name, required=True, default=None):
    value = os.environ.get(name, default)
    if required and not value:
        print(f"ERRO: falta configurar {name}")
        sys.exit(1)
    return value.strip() if isinstance(value, str) else value


def canonical_url(url):
    """Transforma qualquer link da Amazon no formato curto /dp/ASIN."""
    m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", url)
    return f"https://www.amazon.com.br/dp/{m.group(1)}" if m else url


def parse_price(text):
    """'R$ 1.399,99' -> 1399.99"""
    m = re.search(r"(\d{1,3}(?:\.\d{3})+|\d+),(\d{2})", text or "")
    if not m:
        return None
    return float(m.group(1).replace(".", "") + "." + m.group(2))


def fmt(price):
    return "R$ " + f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def is_blocked(html):
    markers = ("validateCaptcha", "Digite os caracteres", "api-services-support@amazon.com")
    return any(m in html for m in markers)


def fetch(url):
    session = requests.Session()
    for attempt in range(3):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200 and not is_blocked(r.text):
                return r.text
            print(f"Tentativa {attempt + 1}: bloqueado ou erro (HTTP {r.status_code})")
        except requests.RequestException as e:
            print(f"Tentativa {attempt + 1}: erro de rede ({e})")
        time.sleep(5 + attempt * 10)
    return None


def extract(html):
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else "Produto"

    avail_el = soup.select_one("#availability")
    avail_text = avail_el.get_text(" ", strip=True) if avail_el else ""
    has_buy_button = bool(soup.select_one("#add-to-cart-button, #buy-now-button"))
    in_stock = has_buy_button and "indisponível" not in avail_text.lower()

    price = None
    if in_stock:
        for sel in PRICE_SELECTORS:
            el = soup.select_one(sel)
            if el:
                price = parse_price(el.get_text())
                if price:
                    break

    seller_el = soup.select_one("#sellerProfileTriggerId, #merchantInfoFeature_feature_div .offer-display-feature-text")
    seller = seller_el.get_text(" ", strip=True) if seller_el else ""

    return {"title": title, "in_stock": in_stock, "price": price, "seller": seller}


def telegram(token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        timeout=20,
    )
    if not r.ok:
        print(f"Falha ao enviar Telegram: {r.status_code} {r.text}")
    return r.ok


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    url = canonical_url(env("PRODUCT_URL"))
    max_price = float(env("MAX_PRICE").replace(",", "."))
    token = env("TELEGRAM_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")

    if env("TEST_ALERT", required=False, default="").lower() == "true":
        ok = telegram(token, chat_id, f"✅ Teste do monitor funcionando!\nProduto: {url}\nAviso quando ≤ {fmt(max_price)}")
        print("Mensagem de teste enviada." if ok else "Não consegui mandar o teste — confira o token e o chat id.")
        # segue e faz a checagem normal também, pra você ver no log se a Amazon respondeu

    state = load_state()
    html = fetch(url)

    if html is None:
        state["blocked_streak"] = state.get("blocked_streak", 0) + 1
        print(f"[{now()}] Amazon bloqueou a consulta ({state['blocked_streak']}x seguidas).")
        if state["blocked_streak"] >= BLOCK_WARNING_AFTER and not state.get("block_warned"):
            telegram(token, chat_id, "⚠️ O monitor está sendo bloqueado pela Amazon há ~2h. Pode ser temporário; se continuar, veja o plano B no guia.")
            state["block_warned"] = True
        save_state(state)
        return

    state["blocked_streak"] = 0
    state["block_warned"] = False

    info = extract(html)
    price, in_stock = info["price"], info["in_stock"]
    status = f"{fmt(price)}" if price else ("em estoque, preço não encontrado" if in_stock else "fora de estoque")
    print(f"[{now()}] {info['title'][:60]} | {status} | vendedor: {info['seller'] or '?'}")

    is_deal = in_stock and price is not None and price <= max_price
    last_alert = state.get("alerted_price")

    if is_deal and (last_alert is None or price < last_alert):
        msg = (
            f"🔔 Preço bom na Amazon!\n\n{info['title'][:120]}\n\n"
            f"💰 {fmt(price)} (seu limite: {fmt(max_price)})\n"
            + (f"🏪 Vendedor: {info['seller']}\n" if info["seller"] else "")
            + f"\n👉 {url}"
        )
        if telegram(token, chat_id, msg):
            state["alerted_price"] = price
            print("Alerta enviado!")
    elif not is_deal and last_alert is not None:
        # saiu do preço bom: na próxima vez que voltar, avisa de novo
        state["alerted_price"] = None

    state["last_check"] = now()
    state["last_status"] = status
    save_state(state)


if __name__ == "__main__":
    main()
