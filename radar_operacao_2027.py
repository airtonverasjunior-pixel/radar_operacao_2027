import json
import os
import re
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional
import requests

# ==============================================================================
# 1. CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ==============================================================================
STATE_FILE = "radar_state.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEATS_AERO_API_KEY = os.getenv("SEATS_AERO_API_KEY", "")

# Credenciais do Apify
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")
# Exemplo de ID: "usuario~nome-do-actor" (use til ~ em vez de barra /)
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "")

# Defina MODO_SIMULACAO=true apenas para testes locais com dados fictícios
MODO_SIMULACAO = os.getenv("MODO_SIMULACAO", "false").lower() == "true"

CONFIG_OPERACAO = {
    "operacao": "Expedicao-Familia-2027",
    "passageiros": 3,
    "saldo_pontos_disponivel": 800000,
    "linha_vermelha_emissao": "2027-04-30",
    "limiar_gatilho1_pct": 12.0,
    "limiar_gatilho1_sigma": 1.5,
    "confirmacao_ausencia": 2,
    # Tetos de alerta em BRL para o total dos 3 passageiros (ida e volta)
    "teto_brl_europa_total": 13500.00,
    "teto_brl_china_total": 22000.00,
}

# Watchlists de Milhas (Seats.aero / LATAM Pass)
WATCHLISTS_MILHAS = [
    {
        "watchlist_id": "GRU-EUR-eco-2027Q3-miles",
        "origem": "GRU",
        "destinos_alvo": ["MAD", "LIS", "PAR", "ROM", "MIL", "FRA"],
        "open_jaw_permitido": True,
        "cabine": "economy",
        "duracao_dias": 17,
        "janela_datas": {"inicio": "2027-06-30", "fim": "2027-07-17"},
        "moeda": "LATAM_PASS_PTS",
        "tabela_dinamica_teto_trecho": 75000,
        "tabela_fixa_ref_trecho": 70800,
        "parceiras_prioritarias": ["LA", "IB", "LH"],
        "aeronaves_preferenciais": ["B787", "A350"],
        "elegivel_upgrade_cupom": True,
        "limiar_gatilho1_pct": 12.0,
        "limiar_gatilho1_sigma": 1.5,
        "alerta_gatilho2": True,
        "alerta_gatilho3": True,
        "confirmacao_ausencia": 2,
    },
    {
        "watchlist_id": "GRU-CHN-eco-2027Q3-miles",
        "origem": "GRU",
        "destinos_alvo": ["PEK", "SHA"],
        "conexao_obrigatoria": "DOH",
        "cabine": "economy",
        "duracao_dias": 17,
        "janela_datas": {"inicio": "2027-06-30", "fim": "2027-07-17"},
        "moeda": "LATAM_PASS_PTS",
        "tabela_dinamica_teto_trecho": None,
        "tabela_fixa_ref_trecho": 125500,
        "parceiras_prioritarias": ["QR"],
        "aeronaves_preferenciais": ["A350"],
        "elegivel_upgrade_cupom": False,
        "limiar_gatilho1_pct": 12.0,
        "limiar_gatilho1_sigma": 1.5,
        "alerta_gatilho2": True,
        "alerta_gatilho3": True,
        "confirmacao_ausencia": 2,
    },
]

# Payload de entrada para o Actor do Apify (Tarifas Pagantes em BRL)
APIFY_INPUT_PAYLOAD = {
    "watchlist": [
        {
            "origin": "GRU",
            "destination": dest,
            "departureDates": ["2027-06-29", "2027-06-30", "2027-07-01"],
            "returnDates": ["2027-07-16", "2027-07-17", "2027-07-18"],
            "tripType": "round_trip",
            "cabinClass": "economy",
            "adults": 3,
            "currency": "BRL",
            "market": "BR",
            "maxStops": 1,
        }
        for dest in ["MAD", "LIS", "FRA", "PEK", "SHA"]
    ],
    "mode": "monitor",
    "includeAllItineraries": True,
    "maxResultsPerRoute": 30,
    "sortBy": "price",
    "includePriceInsights": True,
    "includeFlightNumbers": True,
    "includeLayovers": True,
    "proxyConfiguration": {
        "useApifyProxy": True,
        "apifyProxyGroups": ["RESIDENTIAL"],
        "apifyProxyCountry": "BR",
    },
}

CIAS_PRIORITARIAS = {
    "LATAM": ["LA", "LATAM", "LATAM AIRLINES"],
    "IBERIA": ["IB", "IBERIA"],
    "LUFTHANSA": ["LH", "LUFTHANSA"],
    "QATAR": ["QR", "QATAR", "QATAR AIRWAYS"],
}


# ==============================================================================
# 2. GESTÃO DE ESTADO E NOTIFICAÇÃO (TELEGRAM)
# ==============================================================================
def carregar_estado() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"historico_precos": {}, "ausencias_consecutivas": {}}


def salvar_estado(estado: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)


def enviar_alerta(mensagem: str) -> None:
    print(f"\n[ALERTA TELEGRAM]\n{mensagem}\n" + "-" * 50)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mensagem,
            "parse_mode": "Markdown",
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Erro ao enviar mensagem ao Telegram: {e}")


# ==============================================================================
# 3. MOTOR 1: MILHAS (SEATS.AERO / LATAM PASS)
# ==============================================================================
def buscar_voos_seats_aero(watchlist: Dict[str, Any]) -> List[Dict[str, Any]]:
    if SEATS_AERO_API_KEY:
        headers = {"Partner-Authorization": SEATS_AERO_API_KEY}
        params = {
            "origin_airport": watchlist["origem"],
            "destination_airport": ",".join(watchlist["destinos_alvo"]),
            "start_date": watchlist["janela_datas"]["inicio"],
            "end_date": watchlist["janela_datas"]["fim"],
            "cabin": watchlist["cabine"],
        }
        try:
            resp = requests.get(
                "https://seats.aero/partnerapi/search",
                headers=headers,
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json().get("data", [])
        except requests.RequestException as e:
            enviar_alerta(f"⚠️ *Erro na chamada do Seats.aero ({watchlist['watchlist_id']}):* `{e}`")
            return []

    if MODO_SIMULACAO and watchlist["watchlist_id"] == "GRU-EUR-eco-2027Q3-miles":
        return [
            {
                "rota": "GRU-MAD",
                "data": "2027-07-01",
                "cia": "LA",
                "aeronave": "B787",
                "pontos_por_trecho": 72000,
                "assentos_disponiveis": 3,
                "tipo_tarifa": "dinamica",
            }
        ]

    return []


def avaliar_watchlist_milhas(watchlist: Dict[str, Any], estado: Dict[str, Any]) -> None:
    if not SEATS_AERO_API_KEY and not MODO_SIMULACAO:
        return

    wid = watchlist["watchlist_id"]
    pax_necessarios = CONFIG_OPERACAO["passageiros"]
    voos = buscar_voos_seats_aero(watchlist)

    voos_validos = [
        v for v in voos
        if v.get("assentos_disponiveis", 0) >= pax_necessarios
        and v.get("cia") in watchlist["parceiras_prioritarias"]
    ]

    if not voos_validos:
        ausencias = estado["ausencias_consecutivas"].get(wid, 0) + 1
        estado["ausencias_consecutivas"][wid] = ausencias
        if ausencias == watchlist["confirmacao_ausencia"]:
            print(
                f"[{wid}] Ausência confirmada ({ausencias} leituras consecutivas "
                f"sem bloco de {pax_necessarios} assentos)."
            )
        return

    estado["ausencias_consecutivas"][wid] = 0

    for voo in voos_validos:
        chave_rota = f"MILHAS:{wid}:{voo['rota']}:{voo['cia']}"
        preco = voo["pontos_por_trecho"]
        historico = estado["historico_precos"].setdefault(chave_rota, [])
        motivos_alerta = []

        # Gatilho 1: Queda Percentual ou Desvio Padrão (Sigma)
        if len(historico) >= 3:
            media = statistics.mean(historico)
            desvio = statistics.stdev(historico)
            queda_pct = ((media - preco) / media) * 100

            if queda_pct >= watchlist["limiar_gatilho1_pct"]:
                motivos_alerta.append(
                    f"Gatilho 1: Queda de {queda_pct:.1f}% vs média ({media:,.0f} pts)"
                )
            elif desvio > 0 and (media - preco) >= (watchlist["limiar_gatilho1_sigma"] * desvio):
                motivos_alerta.append(
                    f"Gatilho 1: Desvio abaixo de {watchlist['limiar_gatilho1_sigma']}σ"
                )

        # Gatilho 2: Rompimento do Teto Dinâmico ou Tabela Fixa
        if watchlist["alerta_gatilho2"]:
            teto_din = watchlist.get("tabela_dinamica_teto_trecho")
            ref_fixa = watchlist.get("tabela_fixa_ref_trecho")

            if voo.get("tipo_tarifa") == "dinamica" and teto_din and preco <= teto_din:
                motivos_alerta.append(
                    f"Gatilho 2: Dinâmica abaixo do teto de {teto_din:,} pts (Elegível a Upgrade)"
                )
            elif voo.get("tipo_tarifa") == "fixa" and ref_fixa and preco <= ref_fixa * 1.20:
                motivos_alerta.append(f"Gatilho 2: Tabela Fixa liberada ({preco:,} pts)")

        # Gatilho 3: Bloco de 3 Assentos + Aeronave Preferencial
        if watchlist["alerta_gatilho3"] and motivos_alerta:
            bonus_aero = (
                " | Aeronave Preferencial (6.000 pés)"
                if voo.get("aeronave") in watchlist["aeronaves_preferenciais"]
                else ""
            )
            motivos_alerta.append(
                f"Gatilho 3: {voo['assentos_disponiveis']} assentos confirmados{bonus_aero}"
            )

        historico.append(preco)
        estado["historico_precos"][chave_rota] = historico[-30:]

        if motivos_alerta:
            custo_total_ida = preco * pax_necessarios
            msg = (
                f"✈️ *ALERTA DE MILHAS: {wid}*\n"
                f"• *Rota:* {voo['rota']} ({voo['cia']} - {voo.get('aeronave', 'N/A')})\n"
                f"• *Data:* {voo.get('data', 'N/A')}\n"
                f"• *Tarifa:* {preco:,} pts/trecho (Total 3 pax: {custo_total_ida:,} pts)\n"
                f"• *Assentos:* {voo['assentos_disponiveis']}\n\n"
                f"*Gatilhos:*\n- " + "\n- ".join(motivos_alerta)
            )
            enviar_alerta(msg)


# ==============================================================================
# 4. MOTOR 2: APIFY (TARIFAS PAGANTES EM BRL — AUTOADAPTÁVEL)
# ==============================================================================
def _limpar_preco(valor: Any) -> Optional[float]:
    """Converte strings como 'R$ 12.450,00' ou '12450' em float."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, dict):
        for k in ("amount", "raw", "value", "total", "price"):
            if k in valor:
                return _limpar_preco(valor[k])
        return None
    texto = str(valor).strip()
    limpo = re.sub(r"[^\d.,]", "", texto)
    if not limpo:
        return None
    if "," in limpo and "." in limpo:
        limpo = limpo.replace(".", "").replace(",", ".")
    elif "," in limpo:
        limpo = limpo.replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return None


def _buscar_chave(item: Dict[str, Any], candidatos: List[str], padrao: Any = None) -> Any:
    """Procura o primeiro campo existente entre vários nomes possíveis."""
    for chave in candidatos:
        if chave in item and item[chave] not in (None, "", []):
            return item[chave]
    return padrao


def identificar_cia_prioritaria(cia_raw: Any) -> str:
    """Normaliza string ou lista de cias e identifica se está entre as 4 prioritárias."""
    if isinstance(cia_raw, list):
        texto = " ".join(str(c) for c in cia_raw).upper()
    else:
        texto = str(cia_raw or "").upper()

    for nome_padrao, termos in CIAS_PRIORITARIAS.items():
        if any(termo in texto for termo in termos):
            return nome_padrao
    return ""


def extrair_campos_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mapeamento autoadaptável para Actors de voos do Apify.
    Identifica automaticamente chaves planas ou aninhadas (flights/legs/segments).
    """
    preco_raw = _buscar_chave(
        item, ["price", "totalPrice", "total_price", "fare", "amount", "formattedPrice"]
    )
    preco = _limpar_preco(preco_raw)

    trechos = _buscar_chave(item, ["flights", "legs", "segments", "outbound"], [])
    primeiro_trecho = trechos[0] if isinstance(trechos, list) and trechos else {}

    origem = _buscar_chave(
        item, ["origin", "departureAirport", "departure_airport", "from", "originCode"]
    ) or _buscar_chave(primeiro_trecho, ["departure_airport", "origin", "from"], "GRU")
    if isinstance(origem, dict):
        origem = origem.get("id") or origem.get("code") or "GRU"

    destino = _buscar_chave(
        item, ["destination", "arrivalAirport", "arrival_airport", "to", "destinationCode"]
    ) or _buscar_chave(primeiro_trecho, ["arrival_airport", "destination", "to"], "UNK")
    if isinstance(destino, dict):
        destino = destino.get("id") or destino.get("code") or "UNK"

    cia_raw = _buscar_chave(
        item, ["airline", "airlines", "carrier", "carriers", "operatingAirline"]
    ) or _buscar_chave(primeiro_trecho, ["airline", "carrier"], "N/A")

    ida = _buscar_chave(
        item, ["departureDate", "departure_date", "outboundDate", "date"]
    ) or _buscar_chave(primeiro_trecho, ["departure_time", "departureDate"], "N/A")

    volta = _buscar_chave(
        item, ["returnDate", "return_date", "inboundDate"], "N/A"
    )

    paradas = _buscar_chave(
        item, ["stops", "stopCount", "totalStops", "layoversCount"], "N/A"
    )
    if paradas == "N/A" and isinstance(trechos, list) and trechos:
        paradas = max(0, len(trechos) - 1)

    insight = _buscar_chave(item, ["priceInsight", "price_insight", "priceLevel"], "")

    return {
        "origem": str(origem).upper(),
        "destino": str(destino).upper(),
        "preco": preco,
        "cia_raw": cia_raw,
        "ida": ida,
        "volta": volta,
        "paradas": paradas,
        "insight": insight,
    }


def buscar_voos_apify() -> List[Dict[str, Any]]:
    if not APIFY_API_TOKEN or not APIFY_ACTOR_ID:
        print("[APIFY] Credenciais não configuradas. Pulando varredura Apify.")
        return []

    url = (
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}"
        f"/run-sync-get-dataset-items?token={APIFY_API_TOKEN}"
    )

    try:
        print(f"[APIFY] Disparando Actor {APIFY_ACTOR_ID}...")
        resp = requests.post(url, json=APIFY_INPUT_PAYLOAD, timeout=300)
        resp.raise_for_status()
        itens = resp.json()
        print(f"[APIFY] {len(itens)} itinerários retornados.")
        return itens
    except requests.RequestException as e:
        enviar_alerta(f"⚠️ *Erro na chamada do Apify:* `{e}`")
        return []


def processar_resultados_apify(itens: List[Dict[str, Any]], estado: Dict[str, Any]) -> None:
    if not APIFY_API_TOKEN or not APIFY_ACTOR_ID:
        return

    if not itens:
        ausencias = estado["ausencias_consecutivas"].get("APIFY_GERAL", 0) + 1
        estado["ausencias_consecutivas"]["APIFY_GERAL"] = ausencias
        if ausencias == CONFIG_OPERACAO["confirmacao_ausencia"]:
            enviar_alerta(
                "⚠️ *Radar Apify:* 2 leituras consecutivas sem retorno de itinerários. "
                "Verifique os créditos ou o status do Actor."
            )
        return

    estado["ausencias_consecutivas"]["APIFY_GERAL"] = 0

    melhores: Dict[str, Dict[str, Any]] = {}

    for item in itens:
        dados = extrair_campos_item(item)
        if dados["preco"] is None:
            continue

        cia_prio = identificar_cia_prioritaria(dados["cia_raw"])
        rota = f"{dados['origem']}-{dados['destino']}"
        chave_grupo = f"{rota}:{'PRIO' if cia_prio else 'OUTRAS'}"

        if chave_grupo not in melhores or dados["preco"] < melhores[chave_grupo]["preco"]:
            dados["rota"] = rota
            dados["cia_prioritaria"] = cia_prio
            melhores[chave_grupo] = dados

    # Autodiagnóstico caso o formato do Actor seja diferente do padrão
    if not melhores and itens:
        chaves_detectadas = list(itens[0].keys())
        enviar_alerta(
            f"🔍 *Autodiagnóstico Apify:* {len(itens)} itens recebidos, mas nenhum preço foi lido.\n"
            f"*Chaves do 1º item:* `{chaves_detectadas}`"
        )
        return

    for chave_grupo, dados in melhores.items():
        chave_hist = f"APIFY:{chave_grupo}"
        preco = dados["preco"]
        historico = estado["historico_precos"].setdefault(chave_hist, [])
        motivos = []

        # Gatilho 1: Queda percentual ou desvio padrão (Sigma)
        if len(historico) >= 3:
            media = statistics.mean(historico)
            desvio = statistics.stdev(historico)
            queda_pct = ((media - preco) / media) * 100

            if queda_pct >= CONFIG_OPERACAO["limiar_gatilho1_pct"]:
                motivos.append(f"Queda de {queda_pct:.1f}% vs média (R$ {media:,.2f})")
            elif desvio > 0 and (media - preco) >= (CONFIG_OPERACAO["limiar_gatilho1_sigma"] * desvio):
                motivos.append(f"Desvio abaixo de {CONFIG_OPERACAO['limiar_gatilho1_sigma']}σ")

        # Gatilho 2: Rompimento do teto em BRL
        teto = (
            CONFIG_OPERACAO["teto_brl_china_total"]
            if dados["destino"] in ["PEK", "SHA"]
            else CONFIG_OPERACAO["teto_brl_europa_total"]
        )
        if preco <= teto:
            motivos.append(f"Abaixo do teto alvo (R$ {teto:,.2f}) — conferir tabela de milhas!")

        historico.append(preco)
        estado["historico_precos"][chave_hist] = historico[-30:]

        if motivos:
            selo = f"⭐ *CIA PRIORITÁRIA ({dados['cia_prioritaria']})*\n" if dados["cia_prioritaria"] else ""
            msg = (
                f"{selo}"
                f"📉 *ALERTA APIFY: {dados['rota']}*\n"
                f"• *Cia:* {dados['cia_raw']} | *Paradas:* {dados['paradas']}\n"
                f"• *Datas:* {dados['ida']} ➔ {dados['volta']}\n"
                f"• *Total (3 pax):* R$ {preco:,.2f}\n"
                f"• *Por pessoa:* R$ {preco / 3:,.2f}\n"
            )
            if dados["insight"]:
                msg += f"• *Insight:* {dados['insight']}\n"
            msg += "\n*Gatilhos:*\n- " + "\n- ".join(motivos)
            enviar_alerta(msg)


# ==============================================================================
# 5. VERIFICAÇÃO DE LINHA VERMELHA E EXECUÇÃO PRINCIPAL
# ==============================================================================
def verificar_linha_vermelha() -> None:
    hoje = datetime.now()
    limite = datetime.strptime(CONFIG_OPERACAO["linha_vermelha_emissao"], "%Y-%m-%d")
    dias_restantes = (limite - hoje).days
    if 0 <= dias_restantes <= 15:
        enviar_alerta(
            f"🚨 *LINHA VERMELHA:* Faltam {dias_restantes} dias para o prazo limite "
            f"de emissão ({CONFIG_OPERACAO['linha_vermelha_emissao']})."
        )


def main() -> None:
    print(f"[{datetime.now().isoformat()}] Iniciando varredura: {CONFIG_OPERACAO['operacao']}")
    estado = carregar_estado()

    verificar_linha_vermelha()

    # Motor 1: Milhas (Seats.aero)
    for wl in WATCHLISTS_MILHAS:
        avaliar_watchlist_milhas(wl, estado)

    # Motor 2: Tarifas Pagantes (Apify)
    itens_apify = buscar_voos_apify()
    processar_resultados_apify(itens_apify, estado)

    salvar_estado(estado)
    print("Varredura finalizada e estado salvo.")


if __name__ == "__main__":
    main()
