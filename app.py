import asyncio
import json
import re
import time
import aiohttp
import streamlit as st
import pandas as pd

CHECK_INTERVAL = 30
MAX_RETRIES = 5
DELAY_ENTRE_BATCHES = 0.5
MAX_CONCURRENT = 1


def fmt(v):
    """Formata um número com separador de milhar '.' (ex: 1.500.000)."""
    if v is None:
        return ""
    return f"{int(v):,}".replace(",", ".")

async def request_com_retry(session, throttle, method, url, **kwargs):
    for tentativa in range(MAX_RETRIES):
        async with throttle:
            try:
                async with session.request(method, url, **kwargs) as response:
                    status = response.status
                    if status == 429:
                        retry_after = response.headers.get("Retry-After")
                        espera = float(retry_after) if retry_after else DELAY_ENTRE_BATCHES * (2 ** tentativa)
                        print(f"429 em {url} — aguardando {espera:.2f}s (tentativa {tentativa + 1})")
                        await asyncio.sleep(espera)
                        continue
                    texto = await response.text()
                    await asyncio.sleep(DELAY_ENTRE_BATCHES)
                    return status, texto
            except asyncio.TimeoutError:
                espera = DELAY_ENTRE_BATCHES * (2 ** tentativa)
                print(f"Timeout em {url} — tentativa {tentativa + 1}/{MAX_RETRIES}")
                await asyncio.sleep(espera)
            except Exception as e:
                print(f"Erro em {url}: {type(e).__name__} — {e}")
                await asyncio.sleep(DELAY_ENTRE_BATCHES)
    return status, ""

def extrair_dados_items_do_html(response_text, search_word):
    try:
        partes = re.findall(
            r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
            response_text,
            flags=re.DOTALL,
        )
        if partes:
            conteudo = "".join(partes)
            try:
                conteudo = bytes(conteudo, "utf-8").decode("unicode_escape")
            except Exception:
                pass
        else:
            conteudo = response_text
        match = re.search(
            r'"queryParams":\{.*?\},"list":(\[.*?\]),"totalCount":(\d+)',
            conteudo,
            flags=re.DOTALL,
        )
        if not match:
            match = re.search(
                r'\\"queryParams\\":\{.*?\},\\"list\\":(\[.*?\]),\\"totalCount\\":(\d+)',
                response_text,
                flags=re.DOTALL,
            )
            if not match:
                print(f"NÃO ACHOU DADOS PARA {search_word}")
                return []
            lista_json = match.group(1)
            lista_json = bytes(lista_json, "utf-8").decode("unicode_escape")
        else:
            lista_json = match.group(1)
        items = json.loads(lista_json)
        print(f"{search_word}: {len(items)} itens encontrados")
        return items
    except Exception as ex:
        print(f"Erro parseando {search_word}: {ex}")
        return []


def extrair_detalhes_post(response_text):
    """Extrai os detalhes do item da resposta do POST (Next.js RSC)."""
    try:
        match = re.search(
            r'1:(\{"data":.*?"success":true\})',
            response_text,
            re.DOTALL,
        )
        if not match:
            return {}
        json_str = match.group(1)
        detalhe = json.loads(json_str)
        return detalhe.get("data", {})
    except Exception as e:
        print("Erro parseando detalhe:", e)
        return {}


async def buscar_detalhes_item(session, throttle, search_word, svr_id, map_id, ssi):
    """Faz o POST para obter detalhes do item (itemFullName, etc)."""
    url = (
        "https://ro.gnjoylatam.com/pt/intro/shop-search/trading"
        f"?storeType=BUY"
        f"&serverType=FREYA"
        f"&searchWord={search_word}"
        f"&sortType=LOW_PRICE"
    )
    headers = {
        "Accept": "text/x-component",
        "Content-Type": "text/plain;charset=UTF-8",
        "Next-Action": "40272e359ff56df8fda0073807b30ac5f0640bf73d",
        "Origin": "https://ro.gnjoylatam.com",
        "Referer": url,
        "User-Agent": "Mozilla/5.0",
    }
    payload = [
        {
            "type": "store",
            "params": {
                "svrId": svr_id,
                "mapId": map_id,
                "ssi": ssi,
            },
        }
    ]
    try:
        status, texto = await request_com_retry(
            session, throttle, "POST", url, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if status != 200:
            return {}
        return extrair_detalhes_post(texto)
    except Exception as e:
        print(f"Erro detalhes ssi={ssi}: {e}")
        return {}


async def buscar_preco_mais_barato(session, throttle, search_word):
    url = "https://ro.gnjoylatam.com/pt/intro/shop-search/trading"
    params = {
        "storeType": "BUY",
        "serverType": "FREYA",
        "searchWord": search_word,
        "sortType": "LOW_PRICE",
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://ro.gnjoylatam.com/",
        "Origin": "https://ro.gnjoylatam.com",
    }
    status, response_text = await request_com_retry(
        session,
        throttle,
        "GET",
        url,
        params=params,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    )
    if status != 200:
        print(f"Erro {status} para {search_word}")
        return None
    items_raw = extrair_dados_items_do_html(response_text, search_word)
    if not items_raw:
        return None
    primeiro = items_raw[0]

    # Busca detalhes do item (POST) para pegar o nome real (itemFullName)
    detalhes = await buscar_detalhes_item(
        session, throttle, search_word,
        primeiro["svrId"], primeiro["mapId"], primeiro["ssi"]
    )

    return {
        "Item": detalhes.get("itemFullName") or primeiro.get("itemName") or search_word,
        "Preço": primeiro.get("itemPrice"),
        "Quantidade": primeiro.get("itemCnt"),
        "Loja": primeiro.get("storeName"),
        "Vendedor": primeiro.get("itemSellerCharName"),
        "Mapa": detalhes.get("mapName"),
        "X": detalhes.get("xpos"),
        "Y": detalhes.get("ypos"),
    }

async def checar_item(search_word):
    throttle = asyncio.Semaphore(MAX_CONCURRENT)
    async with aiohttp.ClientSession() as session:
        return await buscar_preco_mais_barato(session, throttle, search_word)

defaults = {
    "watching": False,
    "item_name": "",
    "max_price": 0,
    "volume": 50,
    "check_count": 0,
    "found_deal": None,
    "last_result": None,
    "results_history": [],
}
for key, val in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = val

st.set_page_config(page_title="Ragnarok Item Watcher", layout="wide")
st.title("Ragnarok Item Watcher")
st.markdown(
    "Monitore o preço de um item no mercado. "
    "O app verifica a cada **30 segundos** se o menor preço está "
    "abaixo do seu valor alvo."
)
with st.container(border=True):
    col1, col2, col3 = st.columns([2, 2, 2])
    with col1:
        item_name = st.text_input(
            "🔍 Nome do Item",
            value=st.session_state.item_name,
            disabled=st.session_state.watching,
            placeholder="Ex: Espada Certeira",
        )
    with col2:
        max_price = st.number_input(
            "🎯 Preço Máximo (Zeny)",
            min_value=1,
            step=100_000,
            value=st.session_state.max_price or 1_000_000,
            disabled=st.session_state.watching,
        )
    with col3:
        volume = st.slider(
            "🔊 Volume do Alarme",
            min_value=0,
            max_value=100,
            value=st.session_state.volume,
            disabled=st.session_state.watching,
            help="Volume do som de alerta quando o preço alvo for atingido.",
        )

    col_btn1, col_btn2 = st.columns([1, 1])
    with col_btn1:
        if not st.session_state.watching:
            if st.button("▶️ Iniciar Monitoramento", type="primary", use_container_width=True):
                if not item_name.strip():
                    st.error("Informe o nome do item.")
                else:
                    st.session_state.watching = True
                    st.session_state.item_name = item_name.strip()
                    st.session_state.max_price = max_price
                    st.session_state.volume = volume
                    st.session_state.check_count = 0
                    st.session_state.found_deal = None
                    st.session_state.last_result = None
                    st.session_state.results_history = []
                    st.rerun()

    with col_btn2:
        if st.session_state.watching:
            if st.button("⏹️ Parar Monitoramento", use_container_width=True):
                st.session_state.watching = False
                st.session_state.found_deal = None
                st.rerun()
if st.session_state.watching:
    st.divider()
    st.subheader(f"📡 Monitorando: **{st.session_state.item_name}**")
    st.caption(
        f"Alvo: ≤ {fmt(st.session_state.max_price)} Zeny  |  "
        f"Intervalo: {CHECK_INTERVAL}s  |  "
        f"Checagens realizadas: {st.session_state.check_count}"
    )
    st.session_state.check_count += 1

    with st.spinner(f"🔎 Consultando mercado... (checagem #{st.session_state.check_count})"):
        try:
            resultado = asyncio.run(checar_item(st.session_state.item_name))
        except Exception as e:
            st.error(f"Erro ao consultar: {e}")
            resultado = None

    if resultado is None:
        st.warning(f"⚠️ Nenhum resultado encontrado para '{st.session_state.item_name}'.")
    else:
        preco = resultado["Preço"] or 0
        st.session_state.last_result = resultado
        st.session_state.results_history.append(
            {**resultado, "Checagem": st.session_state.check_count}
        )
        metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
        with metric_col1:
            st.metric("💰 Menor Preço", f"{fmt(preco)} Zeny")
        with metric_col2:
            delta = st.session_state.max_price - preco
            st.metric(
                "📉 Diferença para o alvo",
                f"{fmt(abs(delta))} Zeny",
                delta=f"{'abaixo' if delta >= 0 else 'acima'} do alvo",
            )
        with metric_col3:
            st.metric("🏪 Loja", resultado.get("Loja", "-"))
        with metric_col4:
            mapa = resultado.get("Mapa") or "-"
            x = resultado.get("X")
            y = resultado.get("Y")
            coord = f"{mapa} ({x},{y})" if x is not None and y is not None else mapa
            st.metric("🗺️ Local", coord)
        df_exibicao = pd.DataFrame([resultado]).copy()
        df_exibicao["Preço"] = df_exibicao["Preço"].apply(
            lambda v: fmt(v) if pd.notna(v) else ""
        )
        st.dataframe(
            df_exibicao,
            hide_index=True,
            use_container_width=True,
        )
        if preco <= st.session_state.max_price:
            st.session_state.found_deal = resultado
            st.balloons()
            st.success(
                f"## 🎉 ALERTA! \n\n"
                f"**{resultado.get('Item')}** encontrado por **{fmt(preco)} Zeny** "
                f"na loja **{resultado.get('Loja', '-')}**!\n\n"
                f"Vendedor: **{resultado.get('Vendedor', '-')}**  |  "
                f"Quantidade: **{resultado.get('Quantidade', '-')}**\n\n"
                f"🗺️ Local: **{resultado.get('Mapa', '-')} "
                f"({resultado.get('X')}, {resultado.get('Y')})**"
            )
            vol = st.session_state.volume / 100
            st.markdown(
                f"""
                <audio autoplay>
                    <source src="https://github.com/yude-hurdur/ragnarok-item-watcher/blob/main/hey_listen.mp3" type="audio/mpeg">
                </audio>
                <script>
                    document.querySelector('audio').volume = {vol};
                </script>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.info(
                f"⏳ Preço atual ({fmt(preco)} Zeny) ainda está **acima** do alvo "
                f"({fmt(st.session_state.max_price)} Zeny). "
                f"Aguardando próxima verificação..."
            )
    if st.session_state.results_history:
        with st.expander("📋 Histórico de verificações"):
            df_hist = pd.DataFrame(st.session_state.results_history)
            if "Preço" in df_hist.columns:
                df_hist["Preço"] = df_hist["Preço"].apply(
                    lambda v: fmt(v) if pd.notna(v) else ""
                )
            st.dataframe(df_hist, hide_index=True, use_container_width=True)
            st.line_chart(
                df_hist.set_index("Checagem")["Preço"],
                y_label="Preço (Zeny)",
            )
    if st.session_state.watching:
        progress_bar = st.progress(0, text=f"⏰ Próxima verificação em {CHECK_INTERVAL}s...")
        for i in range(CHECK_INTERVAL):
            time.sleep(1)
            remaining = CHECK_INTERVAL - i - 1
            progress_bar.progress(
                (i + 1) / CHECK_INTERVAL,
                text=f"⏰ Próxima verificação em {remaining}s...",
            )
        st.rerun()
elif st.session_state.found_deal:
    st.divider()
    deal = st.session_state.found_deal
    st.success(
        f"## 🎉 Achado! \n\n"
        f"**{deal.get('Item')}** foi encontrado por "
        f"**{fmt(deal.get('Preço', 0))} Zeny** "
        f"após **{st.session_state.check_count}** verificações!\n\n"
        f"🗺️ Local: **{deal.get('Mapa', '-')} "
        f"({deal.get('X')}, {deal.get('Y')})**"
    )
    df_deal = pd.DataFrame([deal]).copy()
    if "Preço" in df_deal.columns:
        df_deal["Preço"] = df_deal["Preço"].apply(lambda v: fmt(v) if pd.notna(v) else "")
    st.dataframe(df_deal, hide_index=True, use_container_width=True)
    vol = st.session_state.volume / 100
    st.markdown(
        f"""
        <audio autoplay>
            <source src="https://github.com/yude-hurdur/ragnarok-item-watcher/blob/main/hey_listen.mp3" type="audio/mpeg">
        </audio>
        <script>
            document.querySelector('audio').volume = {vol};
        </script>
        """,
        unsafe_allow_html=True,
    )
    st.caption("O monitoramento foi pausado. Configure um novo item para reiniciar.")
