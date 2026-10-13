"""
Monitor de preço da Amazon Brasil com alerta no Telegram (vários produtos).

Os produtos ficam no arquivo produtos.txt, um por linha:
    420  https://www.amazon.com.br/dp/B0XXXXXXXX
(preço máximo, espaço, link)

Secrets no GitHub:
  TELEGRAM_TOKEN    token do seu bot (pego no @BotFather)
  TELEGRAM_CHAT_ID  seu id de chat no Telegram
  PRODUCT_URL / MAX_PRICE  (opcionais: só usados se produtos.txt não existir)
  TEST_ALERT        se for "true", manda uma mensagem de teste
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

STATE_FILE = "state.json"
PRODUCTS_FILE = "produtos.txt"
BRT = timezone(timedelta(hours=-3))
BLOCK_WARNING_AFTER = 8  # ~8 execuções seguidas bloqueado -> te avisa uma vez

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


def asin_of(url):
    m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", url)
    return m.group(1) if m else None


def canonical_url(url):
    """Transforma qualquer link da Amazon no formato curto /dp/ASIN."""
    asin = asin_of(url)
    return f"https://www.amazon.com.br/dp/{asin}" if asin else url


def parse_price(text):
    """'R$ 1.399,99' -> 1399.99"""
    m = re.search(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{2}))?", text or "")
    if not m:
        return None
    cents = m.group(2) or "00"
    return float(m.group(1).replace(".", "") + "." + cents)


def fmt(price):
    return "R$ " + f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def load_products():
    """Lê produtos.txt. Cada linha: <preço máximo> <link>. Linhas com # são ignoradas."""
    products = []
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, encoding="utf-8") as f:
            for n, raw in enumerate(f, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                url_match = re.search(r"https?://\S+", line)
                price_text = line[: url_match.start()] if url_match else ""
                max_price = parse_price(price_text)
                if not url_match or max_price is None:
                    print(f"AVISO: linha {n} do {PRODUCTS_FILE} ignorada (formato: 420 https://...): {line}")
                    continue
                products.append({"url": canonical_url(url_match.group(0)), "max_price": max_price})
    else:
        url, price = os.environ.get("PRODUCT_URL"), os.environ.get("MAX_PRICE")
        if url and price:
            products.append({"url": canonical_url(url.strip()), "max_price": parse_price(price)})

    if not products:
        print(f"ERRO: nenhum produto configurado. Adicione linhas no {PRODUCTS_FILE}.")
        sys.exit(1)
    return products


def is_blocked(html):
    markers = ("validateCaptcha", "Digite os caracteres", "api-services-support@amazon.com")
    return any(m in html for m in markers)


def fetch(session, url):
    for attempt in range(3):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200 and not is_blocked(r.text):
                return r.text
            print(f"  tentativa {attempt + 1}: bloqueado ou erro (HTTP {r.status_code})")
        except requests.RequestException as e:
            print(f"  tentativa {attempt + 1}: erro de rede ({e})")
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
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"Falha ao enviar Telegram: {e}")
        return False
    if not r.ok:
        print(f"Falha ao enviar Telegram: {r.status_code} {r.text}")
    return r.ok


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    # compatibilidade com a versão de um produto só
    if "products" not in state:
        state = {"products": {}, "blocked_streak": state.get("blocked_streak", 0), "block_warned": state.get("block_warned", False)}
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_product(session, product, pstate, token, chat_id):
    """Checa um produto. Retorna True se a Amazon respondeu, False se bloqueou."""
    url, max_price = product["url"], product["max_price"]
    html = fetch(session, url)
    if html is None:
        print(f"[{now()}] {url} | Amazon bloqueou a consulta")
        return False

    info = extract(html)
    price, in_stock = info["price"], info["in_stock"]
    status = fmt(price) if price else ("em estoque, preço não encontrado" if in_stock else "fora de estoque")
    print(f"[{now()}] {info['title'][:60]} | {status} (limite {fmt(max_price)}) | vendedor: {info['seller'] or '?'}")

    is_deal = in_stock and price is not None and price <= max_price
    last_alert = pstate.get("alerted_price")

    if is_deal and (last_alert is None or price < last_alert):
        msg = (
            f"🔔 Preço bom na Amazon!\n\n{info['title'][:120]}\n\n"
            f"💰 {fmt(price)} (seu limite: {fmt(max_price)})\n"
            + (f"🏪 Vendedor: {info['seller']}\n" if info["seller"] else "")
            + f"\n👉 {url}"
        )
        if telegram(token, chat_id, msg):
            pstate["alerted_price"] = price
            print("  -> alerta enviado!")
    elif not is_deal and last_alert is not None:
        # saiu do preço bom: na próxima vez que voltar, avisa de novo
        pstate["alerted_price"] = None

    pstate["title"] = info["title"][:80]
    pstate["last_status"] = status
    pstate["last_check"] = now()
    return True


def main():
    token = env("TELEGRAM_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    products = load_products()

    if env("TEST_ALERT", required=False, default="").lower() == "true":
        lines = "\n".join(f"• {p['url']} (≤ {fmt(p['max_price'])})" for p in products)
        ok = telegram(token, chat_id, f"✅ Teste do monitor funcionando!\n\nMonitorando {len(products)} produto(s):\n{lines}")
        print("Mensagem de teste enviada." if ok else "Não consegui mandar o teste — confira o token e o chat id.")

    state = load_state()
    session = requests.Session()
    answered = 0

    for i, product in enumerate(products):
        if i > 0:
            time.sleep(random.uniform(3, 8))  # pausa entre produtos pra não parecer robô
        key = asin_of(product["url"]) or product["url"]
        pstate = state["products"].setdefault(key, {})
        if check_product(session, product, pstate, token, chat_id):
            answered += 1

    if answered == 0:
        state["blocked_streak"] = state.get("blocked_streak", 0) + 1
        if state["blocked_streak"] >= BLOCK_WARNING_AFTER and not state.get("block_warned"):
            telegram(token, chat_id, "⚠️ O monitor está sendo bloqueado pela Amazon há várias checagens seguidas. Pode ser temporário; se continuar, me chama.")
            state["block_warned"] = True
    else:
        state["blocked_streak"] = 0
        state["block_warned"] = False

    save_state(state)


if __name__ == "__main__":
    main()
