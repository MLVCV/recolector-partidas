#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recolector_riot.py — Estadísticas propias de campeones con la API oficial de Riot
==================================================================================
Junta partidas ranked solo/duo de LAS del parche actual y calcula, por campeón y línea:
partidas, win rate, % de línea, pick rate, ban rate y un tier propio (PBI). Genera:

  · lol_data.js    → el HTML lo carga solo al abrir (mismo formato que usaba OP.GG)
  · lol_data.json  → mismos datos, para inspección

Uso (en la carpeta del proyecto, con la clave en riot_key.txt):
  python recolector_riot.py                     # junta partidas sin límite, hasta que lo cortes con Ctrl+C
  python recolector_riot.py --max-partidas 500  # una corrida corta de prueba
  python recolector_riot.py --horas 6           # corta solo a las 6 horas
  python recolector_riot.py --solo-generar      # no pide nada: rearma lol_data.js con lo ya juntado

Se puede cortar con Ctrl+C en cualquier momento: lo juntado queda guardado y la próxima
corrida sigue desde ahí (las partidas del parche se acumulan en datos_riot/).

La clave se lee de riot_key.txt (o de la variable de entorno RIOT_API_KEY). Nunca se imprime.
La clave de desarrollo vence cada 24 h: si aparece "clave vencida", generá una nueva en
https://developer.riotgames.com y pegala en riot_key.txt.

Qué se guarda: el código de cada partida, su duración, el elo del jugador desde el que se encontró,
los campeones con su línea y resultado, y unas pocas estadísticas de cada uno para los síntomas
del tier (ventaja en la línea, oro gastado, daño, etc.; ver CAMPOS_EXTRA). No se guardan nombres
ni identificadores de jugadores.

lol_data.json trae además, por campeón y línea, los promedios de esas estadísticas ("sintomas").
lol_data.js lleva solo, en top, mid y bot, cuántas veces gana y pierde la línea (ganaLinea y
pierdeLinea, desde 100 partidas): la ficha lo muestra como fuerte o débil en early.

Sin dependencias: solo librería estándar de Python 3.8+.
"""
import argparse
import http.client
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
DATOS = CARPETA / "datos_riot"
ARCHIVO_CLAVE = CARPETA / "riot_key.txt"

# LAS: los jugadores se piden al servidor de la plataforma y las partidas al regional.
PLATAFORMA = "la2"
REGIONAL = "americas"
REGION_TXT = "LAS"
# Para probar contra un servidor falso: RIOT_API_BASE="http://127.0.0.1:8765/{host}"
API_BASE = os.environ.get("RIOT_API_BASE", "https://{host}.api.riotgames.com")
COLA = "RANKED_SOLO_5x5"
COLA_ID = 420

DD_VERSIONES = "https://ddragon.leagueoflegends.com/api/versions.json"
DD_CAMPEONES = "https://ddragon.leagueoflegends.com/cdn/{v}/data/en_US/champion.json"
# Nombre en Riot → nombre en la app (el resto coincide)
ALIAS_APP = {"Nunu & Willump": "Nunu"}

TIERS_DIV = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"]
DIVISIONES = ["I", "II", "III", "IV"]
TIERS_APEX = {"MASTER": "masterleagues", "GRANDMASTER": "grandmasterleagues", "CHALLENGER": "challengerleagues"}
ESMERALDA_MAS = {"EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"}

# Grupos de elo que se publican. Criterio propio: cada partida cuenta para el elo del jugador
# desde el que se la encontró (decisión 9 del contexto).
GRUPOS = {
    "all": {"label": "Todos los elos", "tiers": set(TIERS_DIV) | set(TIERS_APEX)},
    "emerald_plus": {"label": "Esmeralda+", "tiers": ESMERALDA_MAS},
}

POSICIONES = {"TOP": "top", "JUNGLE": "jng", "MIDDLE": "mid", "BOTTOM": "bot", "UTILITY": "sup"}

# ── Criterios propios (provisorios, a revisar con datos reales) ──
MIN_PARTIDAS_LINEA = 10    # menos partidas en una línea: no se publica esa línea
MIN_PARTIDAS_TIER = 100    # la escala del tier se mide solo con estos; es también el mínimo por defecto de la app
LINEAS_EARLY = ("top", "mid", "bot")  # líneas donde se publica ganaLinea y pierdeLinea en lol_data.js
# Tier: PBI = (WR − WR promedio de la línea) × pick / (100 − ban). Mide cuánto gana de más
# un campeón según cuánto se lo elige cuando no está baneado. Un pick raro queda cerca de 0.
# Los cortes se miden en desvíos del PBI dentro de la línea, con 0 = promedio.
CORTES_TIER = [(1.0, "S+"), (0.45, "S"), (0.0, "A"), (-0.5, "B"), (-1.0, "C")]  # si no: "D"
PARTIDAS_POR_JUGADOR = 10  # cuántas partidas nuevas se toman de cada jugador, para variar la muestra
DURACION_MINIMA = 300      # partidas de menos de 5 minutos (remakes) no cuentan

# Estadísticas de cada jugador que se guardan para los síntomas 5, 6 y 7 del tier por síntomas
# (Documentacion/tier-por-sintomas.md). Se guardan como lista, en este orden, para ocupar poco.
# Nombres y tipos verificados en la documentación oficial de match-v5 (2026-10-06); Riot no
# describe qué significan los de "challenges": se publica cómo se reparten sus valores
# ("diagnostico" en lol_data.json) para comprobarlo con datos.
CAMPOS_EXTRA = [
    ("lg", "challenges", "laningPhaseGoldExpAdvantage"),        # ventaja de oro y experiencia en la línea
    ("elg", "challenges", "earlyLaningPhaseGoldExpAdvantage"),  # lo mismo, al principio de la línea
    ("cs", "challenges", "maxCsAdvantageOnLaneOpponent"),       # máxima ventaja de súbditos sobre el rival
    ("nv", "challenges", "maxLevelLeadLaneOpponent"),           # máxima ventaja de nivel sobre el rival
    ("gasto", None, "goldSpent"),
    ("pdano", "challenges", "teamDamagePercentage"),            # parte del daño a campeones del equipo
    ("kp", "challenges", "killParticipation"),
    ("precibe", "challenges", "damageTakenOnTeamPercentage"),   # parte del daño recibido por el equipo
    ("cura", None, "totalHealsOnTeammates"),                    # se le suma totalDamageShieldedOnTeammates
    ("cc", None, "timeCCingOthers"),
    ("vision", None, "visionScore"),
    ("obj", None, "damageDealtToObjectives"),
]


def ahora_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log(msg):
    print(msg, flush=True)


# ════════════════════════════ Clave ════════════════════════════
def leer_clave():
    clave = os.environ.get("RIOT_API_KEY", "").strip()
    if not clave and ARCHIVO_CLAVE.exists():
        clave = ARCHIVO_CLAVE.read_text(encoding="utf-8").strip()
    if not clave:
        sys.exit("No encontré la clave. Creá riot_key.txt en esta carpeta y pegá adentro solo tu clave (RGAPI-...).")
    if not clave.startswith("RGAPI-"):
        sys.exit("La clave de riot_key.txt no empieza con RGAPI-. Revisá que hayas pegado la clave completa y nada más.")
    return clave


# ════════════════════════════ Cliente HTTP con límites ════════════════════════════
class ClaveInvalida(Exception):
    pass


class ClienteRiot:
    """Respeta los límites de pedidos de cada servidor (se leen de las respuestas de Riot)."""

    def __init__(self, clave):
        self.clave = clave
        self.limites = defaultdict(lambda: [(20, 1.0), (100, 120.0)])  # los de la clave de desarrollo
        self.historial = defaultdict(deque)  # host → momentos de los pedidos hechos
        self.pedidos = 0

    def _esperar_turno(self, host):
        hist = self.historial[host]
        while True:
            t = time.monotonic()
            espera = 0.0
            for cant, seg in self.limites[host]:
                recientes = [x for x in hist if t - x < seg]
                if len(recientes) >= cant:
                    espera = max(espera, seg - (t - recientes[0]) + 0.05)
            if espera <= 0:
                break
            time.sleep(espera)
        hist.append(time.monotonic())
        ventana = max(seg for _, seg in self.limites[host])
        while hist and time.monotonic() - hist[0] > ventana:
            hist.popleft()

    def _actualizar_limites(self, host, cabeceras):
        txt = cabeceras.get("X-App-Rate-Limit")
        if not txt:
            return
        lim = []
        for parte in txt.split(","):
            try:
                cant, seg = parte.split(":")
                lim.append((int(cant), float(seg)))
            except ValueError:
                return
        if lim:
            self.limites[host] = lim

    def get(self, host, ruta, params=None):
        url = API_BASE.format(host=host) + ruta
        if params:
            url += "?" + urllib.parse.urlencode(params)
        intento = 0
        sin_conexion = 0  # intentos seguidos sin conexión: no cuentan para el límite de 6
        while intento < 6:
            self._esperar_turno(host)
            req = urllib.request.Request(url, headers={"X-Riot-Token": self.clave, "User-Agent": "lol-checklist-recolector/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.pedidos += 1
                    self._actualizar_limites(host, r.headers)
                    datos = json.loads(r.read().decode("utf-8"))
                if sin_conexion:
                    log("  Volvió la conexión; sigo.")
                return datos
            except urllib.error.HTTPError as e:
                self.pedidos += 1
                intento += 1
                if e.code in (401, 403):
                    raise ClaveInvalida()
                if e.code == 404:
                    return None
                if e.code == 429:
                    espera = float(e.headers.get("Retry-After") or 10)
                    log(f"  Riot pidió esperar {espera:.0f} s (límite de pedidos)…")
                    time.sleep(espera + 0.5)
                    continue
                if e.code >= 500:
                    time.sleep(2 ** intento)
                    continue
                raise
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as e:
                # Sin internet (wifi caído, corte de señal): se espera a que vuelva, sin límite.
                # Lo guardado no se pierde y se corta igual con Ctrl+C.
                sin_conexion += 1
                espera = min(2 ** sin_conexion, 30)
                if sin_conexion == 1 or sin_conexion % 10 == 0:
                    log(f"  Sin conexión ({e}); espero a que vuelva y sigo…")
                time.sleep(espera)
        raise RuntimeError(f"No pude completar el pedido después de varios intentos: {ruta}")


# ════════════════════════════ Data Dragon ════════════════════════════
def bajar_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "lol-checklist"}), timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def datos_ddragon():
    version = bajar_json(DD_VERSIONES)[0]
    campeones = bajar_json(DD_CAMPEONES.format(v=version))["data"]
    por_id = {int(c["key"]): ALIAS_APP.get(c["name"], c["name"]) for c in campeones.values()}
    return version, por_id


def parche_de(version):
    """'16.19.712.4321' o '16.19.1' → '16.19'"""
    partes = version.split(".")
    return ".".join(partes[:2]) if len(partes) >= 2 else version


# ════════════════════════════ Población por división ════════════════════════════
def ruta_division(tier, div):
    return f"/lol/league/v4/entries/{COLA}/{tier}/{div}"


def contar_paginas(cliente, tier, div, cache_paginas):
    """Busca la última página con jugadores (búsqueda exponencial + binaria). Devuelve (páginas, jugadores)."""
    def pagina(n):
        clave = (tier, div, n)
        if clave not in cache_paginas:
            cache_paginas[clave] = cliente.get(PLATAFORMA, ruta_division(tier, div), {"page": n}) or []
        return cache_paginas[clave]

    primera = pagina(1)
    if not primera:
        return 0, 0
    tam = len(primera)
    lo, hi = 1, 2
    while pagina(hi):
        lo, hi = hi, hi * 2
    while hi - lo > 1:  # lo tiene datos, hi no
        mid = (lo + hi) // 2
        if pagina(mid):
            lo = mid
        else:
            hi = mid
    return lo, (lo - 1) * tam + len(pagina(lo))


def poblacion(cliente, cache_paginas):
    """Jugadores por división en LAS. Se guarda una semana en datos_riot/poblacion_LAS.json."""
    archivo = DATOS / f"poblacion_{REGION_TXT}.json"
    if archivo.exists():
        guardado = json.loads(archivo.read_text(encoding="utf-8"))
        if time.time() - guardado.get("ts", 0) < 7 * 86400:
            return guardado["divisiones"]
    log("Contando jugadores por división (una vez por semana, tarda unos minutos)…")
    divisiones = {}
    for tier in TIERS_DIV:
        for div in DIVISIONES:
            paginas, jugadores = contar_paginas(cliente, tier, div, cache_paginas)
            divisiones[f"{tier} {div}"] = {"tier": tier, "div": div, "paginas": paginas, "jugadores": jugadores}
            log(f"  {tier} {div}: {jugadores} jugadores")
    for tier, ruta in TIERS_APEX.items():
        liga = cliente.get(PLATAFORMA, f"/lol/league/v4/{ruta}/by-queue/{COLA}") or {}
        n = len(liga.get("entries", []))
        divisiones[tier] = {"tier": tier, "div": None, "paginas": 1, "jugadores": n}
        log(f"  {tier}: {n} jugadores")
    archivo.write_text(json.dumps({"ts": time.time(), "divisiones": divisiones}, indent=1), encoding="utf-8")
    return divisiones


# ════════════════════════════ Muestreo de jugadores ════════════════════════════
class Muestreador:
    """Elige jugadores al azar, con cada división pesada por su cantidad de jugadores."""

    def __init__(self, cliente, divisiones, cache_paginas):
        self.cliente = cliente
        self.divisiones = divisiones
        self.cache = cache_paginas
        self.apex = {}
        self.usados = set()

    def _jugadores_de(self, d):
        if d["div"] is None:  # Master, Grandmaster, Challenger: una sola lista
            if d["tier"] not in self.apex:
                liga = self.cliente.get(PLATAFORMA, f"/lol/league/v4/{TIERS_APEX[d['tier']]}/by-queue/{COLA}") or {}
                self.apex[d["tier"]] = liga.get("entries", [])
            return self.apex[d["tier"]]
        n = random.randint(1, max(1, d["paginas"]))
        clave = (d["tier"], d["div"], n)
        if clave not in self.cache:
            self.cache[clave] = self.cliente.get(PLATAFORMA, ruta_division(d["tier"], d["div"]), {"page": n}) or []
        return self.cache[clave]

    def elegir(self, grupo):
        candidatas = [d for d in self.divisiones.values() if d["tier"] in GRUPOS[grupo]["tiers"] and d["jugadores"] > 0]
        if not candidatas:
            return None, None
        for _ in range(20):
            d = random.choices(candidatas, weights=[c["jugadores"] for c in candidatas])[0]
            libres = [e for e in self._jugadores_de(d) if e.get("puuid") and e["puuid"] not in self.usados]
            if libres:
                e = random.choice(libres)
                self.usados.add(e["puuid"])
                return e["puuid"], d["tier"]
        return None, None


# ════════════════════════════ Partidas ════════════════════════════
def archivo_partidas(parche):
    return DATOS / f"partidas_{REGION_TXT}_{parche}.jsonl"


def cargar_partidas(parche):
    arch = archivo_partidas(parche)
    partidas = []
    if arch.exists():
        with arch.open(encoding="utf-8") as f:  # línea por línea: con las estadísticas extra el archivo pesa más
            for linea in f:
                if not linea.strip():
                    continue
                try:
                    partidas.append(json.loads(linea))
                except json.JSONDecodeError:
                    pass  # una línea cortada por un corte de luz no rompe todo
    return partidas


def datos_extra(p):
    """Las estadísticas de CAMPOS_EXTRA de un jugador, en orden. None si Riot no la trae."""
    ch = p.get("challenges") or {}
    valores = []
    for corto, donde, campo in CAMPOS_EXTRA:
        v = (ch if donde == "challenges" else p).get(campo)
        if corto == "cura" and v is not None:
            v += p.get("totalDamageShieldedOnTeammates") or 0
        if isinstance(v, float):
            v = round(v, 3)
        valores.append(v if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
    return valores


def resumir_partida(m, parche, tier, objetivo):
    """Lo mínimo de cada partida. None si no sirve (otra cola, otro parche, remake)."""
    info = (m or {}).get("info") or {}
    if info.get("queueId") != COLA_ID:
        return "otra"
    if parche_de(info.get("gameVersion", "")) != parche:
        return "viejo"
    jugadores = info.get("participants") or []
    if len(jugadores) != 10 or info.get("gameDuration", 0) < DURACION_MINIMA or \
            any(p.get("gameEndedInEarlySurrender") for p in jugadores):
        return "otra"
    picks = []
    for p in jugadores:
        pos = POSICIONES.get(p.get("teamPosition") or "")
        if pos:
            picks.append([p.get("championId"), pos, 1 if p.get("win") else 0, datos_extra(p)])
    bans = sorted({b.get("championId") for t in info.get("teams", []) for b in t.get("bans", [])
                   if b.get("championId", -1) > 0})
    return {"id": m["metadata"]["matchId"], "o": objetivo, "t": tier, "d": info.get("gameDuration"), "c": picks, "b": bans}


def recolectar(cliente, parche, max_nuevas, horas, cada_cuanto_generar, generar):
    DATOS.mkdir(exist_ok=True)
    previas = cargar_partidas(parche)
    vistas = {p["id"] for p in previas}
    # Cuántas partidas tiene cada grupo: siempre se busca para el que va más atrás
    cuenta = {g: sum(1 for p in previas if en_grupo(p, g)) for g in GRUPOS}
    del previas  # no hace falta tenerlas en memoria mientras se junta
    descartadas = set()
    log(f"Parche {parche} · {len(vistas)} partidas ya guardadas de corridas anteriores.")

    cache_paginas = {}
    divisiones = poblacion(cliente, cache_paginas)
    muestreo = Muestreador(cliente, divisiones, cache_paginas)
    desde = int(time.time()) - 21 * 86400  # un parche dura unas 2 semanas: 3 de margen
    limite_tiempo = time.time() + horas * 3600 if horas else None
    nuevas, inicio = 0, time.time()
    fallos_seguidos = 0

    with archivo_partidas(parche).open("a", encoding="utf-8") as salida:
        while nuevas < max_nuevas and (not limite_tiempo or time.time() < limite_tiempo):
            objetivo = min(cuenta, key=cuenta.get)  # el grupo con menos partidas
            puuid, tier = muestreo.elegir(objetivo)
            if not puuid:
                fallos_seguidos += 1
                if fallos_seguidos >= 30:
                    log("No consigo jugadores de la lista de Riot (¿cambió la API?). Corto acá.")
                    break
                continue
            fallos_seguidos = 0
            ids = cliente.get(REGIONAL, f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                              {"queue": COLA_ID, "type": "ranked", "startTime": desde, "start": 0, "count": 20}) or []
            tomadas = 0
            for mid in ids:
                if mid in vistas or mid in descartadas:
                    continue
                r = resumir_partida(cliente.get(REGIONAL, f"/lol/match/v5/matches/{mid}"), parche, tier, objetivo)
                if r == "viejo":
                    break  # vienen de la más nueva a la más vieja: el resto también es de otro parche
                if r == "otra" or r is None:
                    descartadas.add(mid)
                    continue
                salida.write(json.dumps(r, separators=(",", ":")) + "\n")
                salida.flush()
                vistas.add(mid)
                for g in GRUPOS:
                    cuenta[g] += en_grupo(r, g)
                nuevas += 1
                tomadas += 1
                if nuevas % 25 == 0:
                    ritmo = nuevas / max(1, time.time() - inicio) * 3600
                    log(f"  {nuevas} partidas nuevas ({len(vistas)} en total) · {ritmo:.0f} por hora · {cliente.pedidos} pedidos")
                if nuevas % cada_cuanto_generar == 0:
                    generar()
                if tomadas >= PARTIDAS_POR_JUGADOR or nuevas >= max_nuevas:
                    break
    return nuevas


# ════════════════════════════ Estadísticas ════════════════════════════
def pbi(wr, wr_linea, pick, ban):
    return (wr - wr_linea) * pick / max(100 - ban, 1)


def poner_tiers(linea):
    """Asigna el tier a cada campeón de una línea según su PBI comparado con el resto de la línea.
    La escala sale de los que tienen MIN_PARTIDAS_TIER o más, pero el tier se publica para todos:
    cada usuario elige en la app desde cuántas partidas mostrarlo."""
    con_tier = [e for e in linea.values() if e["play"] >= MIN_PARTIDAS_TIER]
    valores = [e["_pbi"] for e in con_tier]
    escala = (sum(v * v for v in valores) / len(valores)) ** 0.5 if valores else 0
    for e in linea.values():
        p = e.pop("_pbi")
        e["tier"] = ""
        if not escala:
            continue
        e["tier"] = next((t for corte, t in CORTES_TIER if p / escala >= corte), "D")


def en_grupo(p, grupo):
    if grupo == "all":
        return p.get("o") == "all"  # solo lo muestreado en proporción a toda la población
    return p.get("t") in GRUPOS[grupo]["tiers"]


def calcular(partidas, nombres, grupo):
    sel = [p for p in partidas if en_grupo(p, grupo)]
    n = len(sel)
    jugadas = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # campeón → línea → [partidas, victorias]
    por_linea_total = defaultdict(lambda: [0, 0])  # línea → [apariciones, victorias], todos los campeones
    bans = defaultdict(int)
    for p in sel:
        for cid, pos, gano, *_ in p["c"]:  # las partidas viejas no traen las estadísticas extra
            c = jugadas[cid][pos]
            c[0] += 1
            c[1] += gano
            por_linea_total[pos][0] += 1
            por_linea_total[pos][1] += gano
        for cid in p["b"]:
            bans[cid] += 1
    wr_linea = {ln: (v[1] / v[0] * 100 if v[0] else 50.0) for ln, v in por_linea_total.items()}
    lineas = {ln: {} for ln in POSICIONES.values()}
    for cid, por_linea in jugadas.items():
        nombre = nombres.get(cid)
        if not nombre:
            continue
        total = sum(v[0] for v in por_linea.values())
        for ln, (pj, pg) in por_linea.items():
            if pj < MIN_PARTIDAS_LINEA:
                continue
            wr = pg / pj * 100
            pick = pj / n * 100 if n else 0
            ban = bans[cid] / n * 100 if n else 0
            lineas[ln][nombre] = {
                "wr": round(wr, 2),
                "tier": "",
                "lanePct": round(pj / total * 100, 2),
                "play": pj,
                "pick": round(pick, 2),
                "ban": round(ban, 2),
                "dif": round(wr - wr_linea[ln], 2),  # WR menos el WR promedio de la línea
                "_pbi": pbi(wr, wr_linea[ln], pick, ban),
            }
    for ln in lineas:
        poner_tiers(lineas[ln])
    for ln in lineas:  # ordenadas por partidas, como las mostraba OP.GG
        lineas[ln] = dict(sorted(lineas[ln].items(), key=lambda kv: -kv[1]["play"]))
    return n, lineas


def calcular_sintomas(partidas, nombres, grupo):
    """Promedios de CAMPOS_EXTRA por campeón y línea, solo con las partidas que los traen.
    El rival de línea es el de la misma línea en el otro equipo. "Ganó la línea" = lg mayor que 0
    (supuesto: el "diagnostico" muestra si lg es una marca de 0 o 1)."""
    i = {corto: n for n, (corto, _, _) in enumerate(CAMPOS_EXTRA)}
    filas = defaultdict(list)  # (campeón, línea) → [(ganó, extra, extra del rival, minutos)]
    diagnostico = {k: Counter() for k in ("lg", "elg", "nv")}
    con_datos = 0
    for p in partidas:
        minutos = (p.get("d") or 0) / 60
        jugadores = [c for c in p["c"] if len(c) > 3 and c[3]]
        if not en_grupo(p, grupo) or not jugadores or minutos <= 0:
            continue
        con_datos += 1
        for cid, pos, gano, x in jugadores:
            rivales = [r for r in jugadores if r[1] == pos and r[2] != gano]
            filas[(cid, pos)].append((gano, x, rivales[0][3] if len(rivales) == 1 else None, minutos))
            for k, cuenta in diagnostico.items():
                if x[i[k]] is not None:
                    cuenta[x[i[k]]] += 1

    def media(lista, campo, escala=1.0, por_minuto=False):
        vals = [x[i[campo]] / (m if por_minuto else 1) for _, x, _, m in lista if x[i[campo]] is not None]
        return round(sum(vals) / len(vals) * escala, 2) if vals else None

    lineas = {ln: {} for ln in POSICIONES.values()}
    for (cid, pos), lista in filas.items():
        nombre = nombres.get(cid)
        if not nombre or len(lista) < MIN_PARTIDAS_LINEA:
            continue
        con_rival = [(g, rx) for g, _, rx, _ in lista if rx and rx[i["lg"]] is not None]
        pierde = [g for g, rx in con_rival if rx[i["lg"]] > 0]
        con_lg = [(g, x[i["lg"]]) for g, x, _, _ in lista if x[i["lg"]] is not None]
        lineas[pos][nombre] = {
            "n": len(lista),
            "lg": media(lista, "lg"), "elg": media(lista, "elg"),
            "cs": media(lista, "cs"), "nv": media(lista, "nv"),
            "ganaLinea": round(sum(v > 0 for _, v in con_lg) / len(con_lg) * 100, 2) if con_lg else None,
            "pierdeLinea": round(len(pierde) / len(con_rival) * 100, 2) if con_rival else None,
            "nPierde": len(pierde),
            "wrPierde": round(sum(pierde) / len(pierde) * 100, 2) if pierde else None,  # síntoma 6
            "gastoMin": media(lista, "gasto", por_minuto=True),
            "pdano": media(lista, "pdano", 100), "kp": media(lista, "kp", 100),
            "precibe": media(lista, "precibe", 100),
            "curaMin": media(lista, "cura", por_minuto=True), "ccMin": media(lista, "cc", por_minuto=True),
            "visionMin": media(lista, "vision", por_minuto=True), "objMin": media(lista, "obj", por_minuto=True),
        }
    diag = {"partidas_con_datos": con_datos,
            "valores": {k: {str(v): n for v, n in c.most_common(8)} for k, c in diagnostico.items()}}
    return {ln: dict(sorted(f.items(), key=lambda kv: -kv[1]["n"])) for ln, f in lineas.items()}, diag


def generar_archivos(parche, version, nombres):
    partidas = cargar_partidas(parche)
    ts = ahora_iso()
    elos = {}
    for g in GRUPOS:
        n, lineas = calcular(partidas, nombres, g)
        if n:
            elos[g] = {"generated": ts, "region": REGION_TXT, "parche": parche, "partidas": n, "lanes": lineas}
    if not elos:
        log("Todavía no hay partidas para generar lol_data.js.")
        return
    datos = {
        "fuente": f"API de Riot · {REGION_TXT} · parche {parche}",
        "ddragon": version,
        "generated": ts,
        "metodo": {
            "elo": "cada partida cuenta para el elo del jugador desde el que se encontró",
            "min_partidas_linea": MIN_PARTIDAS_LINEA,
            "min_partidas_tier": MIN_PARTIDAS_TIER,
            "tier": f"PBI = (WR − WR de la línea) × pick / (100 − ban); cortes en desvíos dentro de la línea {CORTES_TIER}, si no D",
        },
        "elos": elos,
    }
    # Síntoma 5 (decisión 12): la app muestra si el campeón es fuerte o débil en early, solo en top, mid y bot.
    # En jungla y support la ventaja en la línea mide quién se queda con el oro, no el early.
    sintomas = {g: calcular_sintomas(partidas, nombres, g) for g in elos}
    for g in elos:
        for ln in LINEAS_EARLY:
            for nombre, e in elos[g]["lanes"].get(ln, {}).items():
                s = sintomas[g][0].get(ln, {}).get(nombre)
                if s and s["n"] >= MIN_PARTIDAS_TIER and s["ganaLinea"] is not None and s["pierdeLinea"] is not None:
                    e["ganaLinea"], e["pierdeLinea"] = s["ganaLinea"], s["pierdeLinea"]
    lista_elos = [[g, GRUPOS[g]["label"]] for g in GRUPOS if g in elos]
    js = ("window.LOL_ELOS=" + json.dumps(lista_elos, ensure_ascii=False) + ";\n"
          "window.LOL_LIVE_DATA=" + json.dumps(datos, ensure_ascii=False) + ";\n")
    (CARPETA / "lol_data.js").write_text(js, encoding="utf-8")
    # Los promedios para los síntomas van solo en lol_data.json, para analizarlos fuera de la app
    for g in elos:
        elos[g]["sintomas"], elos[g]["diagnostico"] = sintomas[g]
    (CARPETA / "lol_data.json").write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    resumen = " · ".join(f"{GRUPOS[g]['label']}: {elos[g]['partidas']} partidas" for g in elos)
    log(f"lol_data.js generado ({resumen}).")


# ════════════════════════════ Principal ════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="Estadísticas propias con la API de Riot (LAS).")
    ap.add_argument("--max-partidas", type=int, default=None, help="partidas nuevas a juntar en esta corrida (default: sin límite, hasta Ctrl+C)")
    ap.add_argument("--horas", type=float, default=None, help="cortar después de estas horas")
    ap.add_argument("--parche", default=None, help="parche a juntar, ej. 16.19 (default: el último de Data Dragon)")
    ap.add_argument("--solo-generar", action="store_true", help="no pedir nada: rearmar lol_data.js con lo ya guardado")
    args = ap.parse_args()

    version, nombres = datos_ddragon()
    parche = args.parche or parche_de(version)

    def generar():
        generar_archivos(parche, version, nombres)

    if args.solo_generar:
        generar()
        return

    cliente = ClienteRiot(leer_clave())
    clave_rechazada = False
    if not args.max_partidas and not args.horas:
        log("Sin límite de partidas: cortá con Ctrl+C cuando quieras (lo juntado queda guardado).")
    try:
        nuevas = recolectar(cliente, parche, args.max_partidas or float("inf"), args.horas, 500, generar)
        log(f"Listo: {nuevas} partidas nuevas.")
    except ClaveInvalida:
        clave_rechazada = True
        log("\nRiot rechazó la clave: probablemente venció (dura 24 h). Generá una nueva en "
            "https://developer.riotgames.com, pegala en riot_key.txt y volvé a correr: sigue desde donde quedó.")
    except KeyboardInterrupt:
        log("\nCortado a mano. Lo juntado quedó guardado.")
    generar()
    if clave_rechazada:
        sys.exit(1)  # para que la corrida en GitHub Actions figure como fallida y se note


if __name__ == "__main__":
    main()
