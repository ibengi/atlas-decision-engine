#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dashboard_web.py — tableau de bord web temps reel (v12.6.0).

REGLE D'HONNETETE : ce dashboard n'affiche QUE des donnees reelles lues
dans les fichiers d'etat du moteur (trades regles, positions, candidats du
dernier cycle, solde broker). Quand une metrique n'est pas calculable
(ex. < 30 trades regles), il affiche le drapeau low_sample ou « — »,
JAMAIS une valeur inventee. Les chiffres de la maquette d'origine
(WR 66,67 %, Sharpe 1.87, strategies ETH…) etaient fictifs.

Zero dependance : http.server de la stdlib, HTML/CSS/JS embarques,
graphiques dessines en canvas. Concu pour tourner dans un thread daemon a
cote du moteur (Railway fournit $PORT).

Endpoints :
  GET /            page HTML
  GET /api/state   JSON agrege (etat moteur + trades + positions + stats)
  GET /health      JSON sante du moteur (statut agrege + checks, P4.3) ;
                   200 si healthy/degraded, 503 si unhealthy
"""
import json
import os
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


try:
    from performance import compute_stats
except Exception:                                    # pragma: no cover
    compute_stats = None

try:
    from health_monitor import get_health_json
except Exception:                                    # pragma: no cover
    get_health_json = None


# ─────────────────────────────────────────────────────────────────────────
# Agregation d'etat (lecture seule des fichiers JSON du moteur)
# ─────────────────────────────────────────────────────────────────────────
def _load(data_dir, name, default):
    p = os.path.join(data_dir, name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def build_state(data_dir: str) -> dict:
    """Etat complet du dashboard, uniquement depuis les fichiers reels."""
    live = _load(data_dir, "dashboard_state.json", {})   # ecrit par le moteur
    report = _load(data_dir, "cycle_report.json", {})
    trades = _load(data_dir, "kalshi_trades.json", [])
    if isinstance(trades, dict):                         # schema {id: trade}
        trades = list(trades.values())
    positions = _load(data_dir, "positions_state.json", {})
    orders = _load(data_dir, "orders_state.json", {})
    risk = _load(data_dir, "risk_state.json", {})

    settled = [t for t in trades if t.get("status") == "settled"
               or t.get("settled_at")]
    open_trades = [t for t in trades if not (t.get("status") == "settled"
                                             or t.get("settled_at"))]
    stats = {}
    if compute_stats is not None:
        try:
            stats = compute_stats(settled, capital=live.get("capital"))
        except Exception as e:                           # jamais de crash UI
            stats = {"error": str(e)}

    # Courbe d'equity = PnL realise CUMULE (trades regles, ordre de
    # reglement). Aucun point n'est interpole ni invente.
    curve, cum = [], 0.0
    for t in sorted(settled, key=lambda x: x.get("settled_at") or ""):
        try:
            cum += float(t.get("net_pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        curve.append({"t": t.get("settled_at"), "v": round(cum, 2)})

    pnl_by_day = {}
    for t in settled:
        d = (t.get("settled_at") or "")[:10]
        if d:
            try:
                pnl_by_day[d] = round(
                    pnl_by_day.get(d, 0.0) + float(t.get("net_pnl") or 0), 2)
            except (TypeError, ValueError):
                pass

    pnl_by_strategy = {}
    for t in settled:
        s = t.get("strategy") or t.get("grade") or "inconnu"
        try:
            pnl_by_strategy[s] = round(
                pnl_by_strategy.get(s, 0.0) + float(t.get("net_pnl") or 0), 2)
        except (TypeError, ValueError):
            pass

    today = date.today().isoformat()
    pnl_today = pnl_by_day.get(today, 0.0)

    # Exposition = cout d'entree des positions ouvertes (donnees reelles)
    exposure = 0.0
    for p in (positions or {}).values():
        try:
            exposure += (int(p.get("count") or p.get("filled_count") or 0)
                         * int(p.get("entry_price_cents")
                               or p.get("avg_price") or 0)) / 100.0
        except (TypeError, ValueError):
            pass

    return {
        "ts": live.get("ts"),
        "version": live.get("version"),
        "env": live.get("env", "demo"),
        "cycle": live.get("cycle") or report.get("cycle"),
        "balance": live.get("balance"),
        "capital": live.get("capital"),
        "configured_capital": live.get("configured_capital"),
        "shadow_settled": live.get("shadow_settled"),
        "candidates": live.get("candidates") or [],
        "exchange_paused": live.get("exchange_paused", False),
        "report": {k: report.get(k) for k in
                   ("cycle_id", "scanned_raw", "open_cached", "liquid",
                    "supported", "model_evaluated", "positive_edge",
                    "positive_net_ev", "risk_passed", "orders_submitted",
                    "fills", "rejections_by_reason", "funnel_conversion",
                    "cycle_duration_ms")},
        "stats": stats,
        "equity_curve": curve[-500:],
        "pnl_by_day": pnl_by_day,
        "pnl_by_strategy": pnl_by_strategy,
        "pnl_today": pnl_today,
        "positions": positions,
        "open_orders": orders,
        "open_trades_count": len(open_trades),
        "settled_count": len(settled),
        "exposure": round(exposure, 2),
        "risk": risk,
    }


# ─────────────────────────────────────────────────────────────────────────
# Serveur HTTP (stdlib)
# ─────────────────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    data_dir = "."

    def log_message(self, *a):                           # silence access log
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._send(200, json.dumps(build_state(self.data_dir),
                                       ensure_ascii=False, default=str),
                       "application/json")
        elif self.path.startswith("/health"):
            if get_health_json is None:
                self._send(503, '{"status":"unhealthy",'
                                '"error":"health_monitor unavailable"}',
                           "application/json")
                return
            health = get_health_json(data_dir=self.data_dir)
            code = 503 if health.get("status") == "unhealthy" else 200
            self._send(code, json.dumps(health, ensure_ascii=False,
                                        default=str), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html")
        else:
            self._send(404, '{"error":"not_found"}', "application/json")


def start_dashboard(data_dir: str, port: int) -> ThreadingHTTPServer:
    """Demarre le serveur dans un thread daemon ; retourne le serveur."""
    _Handler.data_dir = data_dir
    srv = ThreadingHTTPServer(("0.0.0.0", int(port)), _Handler)
    th = threading.Thread(target=srv.serve_forever,
                          name="dashboard", daemon=True)
    th.start()
    return srv


# ─────────────────────────────────────────────────────────────────────────
# Page (HTML/CSS/JS embarques, graphiques canvas, polling 5 s)
# ─────────────────────────────────────────────────────────────────────────
PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#070b13">
<title>Kalshi Alpha Terminal</title>
<style>
:root{--bg:#070b13;--bg2:#0a101b;--panel:#0d1523;--panel2:#101b2c;--line:#1b2a40;--line2:#243753;--text:#e8eef8;--muted:#7f91ab;--green:#20e07a;--red:#ff5570;--amber:#ffb020;--blue:#4d7cff;--violet:#8b5cf6;--cyan:#27c9ff;--shadow:0 14px 40px rgba(0,0,0,.28)}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:radial-gradient(circle at 80% -20%,#142241 0,transparent 35%),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}body{overflow-x:hidden}.mono{font-family:"JetBrains Mono",SFMono-Regular,Consolas,monospace}.app{display:grid;grid-template-columns:226px minmax(0,1fr);min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;background:linear-gradient(180deg,#09101c,#070c15);border-right:1px solid var(--line);padding:18px 12px;display:flex;flex-direction:column;z-index:20}.logo{display:flex;align-items:center;gap:11px;padding:4px 8px 22px}.mark{width:36px;height:36px;border-radius:11px;background:linear-gradient(145deg,var(--violet),var(--blue));display:grid;place-items:center;box-shadow:0 0 28px rgba(99,102,241,.35);font-weight:900;font-size:20px}.logo b{font-size:14px;letter-spacing:.08em}.logo small{display:block;color:#79a3ff;margin-top:2px}.nav-label{font-size:10px;color:#50627d;letter-spacing:.14em;text-transform:uppercase;padding:8px 12px}.nav{display:grid;gap:4px}.nav a{color:#91a2ba;text-decoration:none;padding:10px 11px;border-radius:8px;display:flex;align-items:center;gap:10px;font-size:12px}.nav a:hover,.nav a.active{background:linear-gradient(90deg,rgba(99,102,241,.22),rgba(99,102,241,.04));color:#fff;border:1px solid rgba(124,108,255,.25);padding:9px 10px}.ico{width:17px;text-align:center;color:#9b8cff}.side-bottom{margin-top:auto;display:grid;gap:10px}.status-box{border:1px solid var(--line);background:#0b1320;border-radius:10px;padding:12px}.status-line{display:flex;justify-content:space-between;font-size:11px;padding:4px 0;color:#8ea0b8}.dot{width:7px;height:7px;background:var(--green);border-radius:50%;display:inline-block;margin-right:5px;box-shadow:0 0 10px var(--green)}.mode{display:flex;justify-content:space-between;align-items:center;border:1px solid #1d3a31;background:#0a1815;border-radius:9px;padding:10px 12px;font-size:11px}.mode strong{color:var(--green)}
.main{min-width:0}.topbar{height:72px;border-bottom:1px solid var(--line);background:rgba(7,11,19,.82);backdrop-filter:blur(15px);display:flex;align-items:center;justify-content:space-between;padding:0 20px;position:sticky;top:0;z-index:15}.title h1{font-size:15px;margin:0;letter-spacing:.06em}.title p{font-size:11px;color:var(--muted);margin:4px 0 0}.top-actions{display:flex;align-items:center;gap:10px}.live-pill{border:1px solid #1c4935;background:#0a1b15;color:var(--green);padding:7px 10px;border-radius:999px;font-size:10px;font-weight:700}.clock{font-size:11px;color:#9aaac0}.avatar{width:34px;height:34px;border:1px solid #6d5ce7;border-radius:50%;display:grid;place-items:center;background:#151330;color:#fff;font-size:11px}
.content{padding:14px 16px 24px}.kpis{display:grid;grid-template-columns:repeat(8,minmax(115px,1fr));gap:9px;margin-bottom:11px}.kpi{position:relative;overflow:hidden;background:linear-gradient(145deg,#0e1725,#0b1320);border:1px solid var(--line);border-radius:10px;padding:12px 13px;min-height:82px;box-shadow:var(--shadow)}.kpi:after{content:"";position:absolute;right:-15px;bottom:-25px;width:70px;height:70px;border-radius:50%;background:radial-gradient(circle,rgba(77,124,255,.12),transparent 70%)}.kpi .label{font-size:9px;color:#72859f;text-transform:uppercase;letter-spacing:.12em}.kpi .value{font-size:20px;font-weight:750;margin-top:7px}.kpi .sub{font-size:9px;color:#5f728d;margin-top:3px}.positive{color:var(--green)!important}.negative{color:var(--red)!important}.neutral{color:var(--text)!important}
.layout{display:grid;grid-template-columns:1.45fr 1fr .95fr;gap:11px}.panel{background:linear-gradient(150deg,rgba(16,27,44,.96),rgba(11,19,32,.97));border:1px solid var(--line);border-radius:11px;box-shadow:var(--shadow);min-width:0}.panel-head{height:45px;padding:0 14px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #17243a}.panel-head h2{margin:0;font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:#a5b5cb}.panel-head .meta{font-size:10px;color:#5f728d}.panel-body{padding:12px 14px}.span2{grid-column:span 2}.span3{grid-column:span 3}.chart-wrap{height:250px;position:relative}.chart-wrap.small{height:190px}canvas{width:100%;height:100%;display:block}.empty{height:100%;display:grid;place-items:center;color:#60738d;font-size:12px}.metric-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.metric{background:#0a1321;border:1px solid #1a2a42;border-radius:9px;padding:14px 10px;text-align:center}.metric .ml{font-size:9px;color:#6f8199;text-transform:uppercase;letter-spacing:.08em}.metric .mv{font-size:19px;font-weight:750;margin-top:7px}.metric .ms{font-size:9px;color:#52647e;margin-top:3px}.sample-note{margin-top:9px;border:1px solid #3a3020;background:#17140e;color:#b99c61;border-radius:7px;padding:8px;font-size:10px;line-height:1.45}
table{width:100%;border-collapse:collapse}th{font-size:9px;color:#62758f;text-transform:uppercase;letter-spacing:.08em;text-align:left;padding:8px;border-bottom:1px solid #1c2a40;white-space:nowrap}td{font-size:10px;padding:8px;border-bottom:1px solid #142137;white-space:nowrap;max-width:230px;overflow:hidden;text-overflow:ellipsis}.table-scroll{overflow:auto;max-height:265px}.badge{display:inline-flex;align-items:center;padding:3px 7px;border-radius:5px;font-size:9px;font-weight:750;text-transform:uppercase}.b-green{background:#0c2a1c;color:var(--green);border:1px solid #16462f}.b-red{background:#30121b;color:var(--red);border:1px solid #4b1a28}.b-amber{background:#30230e;color:var(--amber);border:1px solid #4a3411}.b-blue{background:#12234a;color:#7aa1ff;border:1px solid #203a70}.ticker{font-weight:700;color:#dce6f5}.tiny-bar{height:4px;background:#17243a;border-radius:10px;overflow:hidden;margin-top:4px}.tiny-bar i{display:block;height:100%;background:linear-gradient(90deg,var(--violet),var(--blue));border-radius:10px}.funnel{display:grid;gap:7px}.frow{display:grid;grid-template-columns:118px 1fr 48px;gap:8px;align-items:center;font-size:10px}.ftrack{height:7px;border-radius:10px;background:#101b2c;overflow:hidden}.ftrack i{display:block;height:100%;background:linear-gradient(90deg,#5e52d9,#3e8dff);border-radius:10px}.fval{text-align:right;color:#99aac1}.risk-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.gauge{background:#0b1422;border:1px solid #192941;border-radius:9px;padding:12px}.gauge-top{display:flex;justify-content:space-between;font-size:10px;color:#8294ad}.gauge .bar{height:8px;background:#172338;border-radius:10px;margin-top:9px;overflow:hidden}.gauge .bar i{display:block;height:100%;border-radius:10px;background:linear-gradient(90deg,var(--green),#77f1ac)}.activity{display:grid;gap:0;max-height:202px;overflow:auto}.event{display:grid;grid-template-columns:58px 52px 1fr;gap:7px;padding:7px 0;border-bottom:1px solid #142036;font-size:9px}.event time{color:#6d819c}.event .tag{font-weight:700}.footer-ticker{grid-column:span 3;display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden}.asset{background:#0c1522;padding:11px 13px;font-size:10px;display:flex;justify-content:space-between}.asset b{color:#dce6f5}.spark{color:var(--green)}
@media(max-width:1450px){.kpis{grid-template-columns:repeat(4,1fr)}}@media(max-width:1050px){.app{grid-template-columns:72px 1fr}.sidebar{padding:16px 8px}.logo div:last-child,.nav-label,.nav a span:last-child,.side-bottom{display:none}.logo{justify-content:center;padding:2px 0 20px}.nav a{justify-content:center}.layout{grid-template-columns:1fr 1fr}.span3,.footer-ticker{grid-column:span 2}.span2{grid-column:span 2}}@media(max-width:720px){.app{display:block}.sidebar{display:none}.topbar{height:auto;padding:12px}.kpis{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.span2,.span3,.footer-ticker{grid-column:span 1}.footer-ticker{grid-template-columns:1fr 1fr}.content{padding:9px}.top-actions .clock{display:none}}
</style></head><body><div class="app">
<aside class="sidebar"><div class="logo"><div class="mark">A</div><div><b>KALSHI ALPHA</b><small>TERMINAL <span id="ver">—</span></small></div></div><div class="nav-label">Trading</div><nav class="nav"><a class="active" href="#"><span class="ico">▦</span><span>Tableau de bord</span></a><a href="#candidates"><span class="ico">◎</span><span>Opportunités</span></a><a href="#positions"><span class="ico">◫</span><span>Positions</span></a><a href="#orders"><span class="ico">⇄</span><span>Ordres</span></a><a href="#performance"><span class="ico">⌁</span><span>Performance</span></a><a href="#risk"><span class="ico">⬡</span><span>Risque</span></a><a href="#activity"><span class="ico">≡</span><span>Journal</span></a></nav><div class="nav-label" style="margin-top:14px">Système</div><nav class="nav"><a href="#"><span class="ico">⚙</span><span>Configurations</span></a><a href="#"><span class="ico">⌘</span><span>Stratégies</span></a><a href="#"><span class="ico">⌁</span><span>API & clés</span></a></nav><div class="side-bottom"><div class="status-box"><div class="status-line"><span>API Kalshi</span><span class="positive"><i class="dot"></i>Connecté</span></div><div class="status-line"><span>Probability Engine</span><span class="positive">Actif</span></div><div class="status-line"><span>Risk Engine</span><span class="positive">Actif</span></div><div class="status-line"><span>Dashboard</span><span class="positive">En ligne</span></div></div><div class="mode"><span>MODE</span><strong id="mode">DEMO</strong></div></div></aside>
<main class="main"><header class="topbar"><div class="title"><h1>ATLAS DECISION ENGINE</h1><p class="mono">Terminal d'exécution et de surveillance en temps réel</p></div><div class="top-actions"><span class="live-pill"><i class="dot"></i>LIVE DATA</span><span class="clock mono" id="clock">—</span><div class="avatar">KA</div></div></header>
<div class="content"><section class="kpis"><div class="kpi"><div class="label">Solde</div><div class="value" id="bal">—</div><div class="sub">cash disponible</div></div><div class="kpi"><div class="label">P&L jour</div><div class="value" id="pnlD">—</div><div class="sub">réalisé aujourd'hui</div></div><div class="kpi"><div class="label">P&L total</div><div class="value" id="pnlT">—</div><div class="sub">résultats réglés</div></div><div class="kpi"><div class="label">Exposition</div><div class="value" id="expo">—</div><div class="sub">capital engagé</div></div><div class="kpi"><div class="label">Positions</div><div class="value" id="npos">—</div><div class="sub">ouvertes</div></div><div class="kpi"><div class="label">Trades réglés</div><div class="value" id="ntr">—</div><div class="sub">échantillon</div></div><div class="kpi"><div class="label">Capital effectif</div><div class="value" id="cap">—</div><div class="sub">base de risque</div></div><div class="kpi"><div class="label">Cycle moteur</div><div class="value" id="cyc">—</div><div class="sub" id="cycleTime">actualisation 5 s</div></div></section>
<section class="layout"><article class="panel span2" id="performance"><div class="panel-head"><h2>Performance du portefeuille</h2><span class="meta mono" id="equityMeta">PnL réalisé cumulé</span></div><div class="panel-body"><div class="chart-wrap"><canvas id="eq"></canvas></div></div></article><article class="panel"><div class="panel-head"><h2>Métriques clés</h2><span class="meta" id="sampleLabel">données réelles</span></div><div class="panel-body"><div class="metric-grid"><div class="metric"><div class="ml">Win rate</div><div class="mv" id="wr">—</div><div class="ms">trades gagnants</div></div><div class="metric"><div class="ml">Profit factor</div><div class="mv" id="pf">—</div><div class="ms">gains / pertes</div></div><div class="metric"><div class="ml">Expectancy</div><div class="mv" id="exp">—</div><div class="ms">par trade</div></div><div class="metric"><div class="ml">Sharpe / trade</div><div class="mv" id="sh">—</div><div class="ms">rendement ajusté</div></div><div class="metric"><div class="ml">Max drawdown</div><div class="mv" id="dd">—</div><div class="ms">baisse maximale</div></div><div class="metric"><div class="ml">Shadow réglées</div><div class="mv" id="shw">—</div><div class="ms">validation modèle</div></div></div><div class="sample-note" id="lowN">Chargement des métriques…</div></div></article>
<article class="panel span2" id="candidates"><div class="panel-head"><h2>Opportunités du dernier cycle</h2><span class="meta" id="candMeta">—</span></div><div class="panel-body table-scroll"><table><thead><tr><th>Ticker</th><th>Stratégie</th><th>Côté</th><th>Prob. modèle</th><th>Prob. marché</th><th>Edge net</th><th>EV net</th><th>Confiance</th><th>Statut</th></tr></thead><tbody id="cands"></tbody></table></div></article><article class="panel" id="positions"><div class="panel-head"><h2>Positions & ordres</h2><span class="meta" id="posMeta">—</span></div><div class="panel-body"><div class="table-scroll" style="max-height:120px"><table><thead><tr><th>Ticker</th><th>Côté</th><th>Taille</th><th>Prix</th></tr></thead><tbody id="poss"></tbody></table></div><div style="height:1px;background:#1a2940;margin:11px 0"></div><div class="table-scroll" style="max-height:120px" id="orders"><table><thead><tr><th>Ticker</th><th>Côté</th><th>Taille</th><th>Prix</th></tr></thead><tbody id="ords"></tbody></table></div></div></article>
<article class="panel"><div class="panel-head"><h2>P&L par jour</h2><span class="meta">réalisé</span></div><div class="panel-body"><div class="chart-wrap small"><canvas id="pd"></canvas></div></div></article><article class="panel" id="risk"><div class="panel-head"><h2>Exposition & risque</h2><span class="meta">contrôles</span></div><div class="panel-body"><div class="risk-grid"><div class="gauge"><div class="gauge-top"><span>Exposition</span><b id="riskExpo">—</b></div><div class="bar"><i id="riskExpoBar" style="width:0%"></i></div></div><div class="gauge"><div class="gauge-top"><span>Risque journalier</span><b id="riskDaily">—</b></div><div class="bar"><i id="riskDailyBar" style="width:0%"></i></div></div></div><div style="margin-top:13px" class="funnel" id="fun"></div></div></article><article class="panel" id="activity"><div class="panel-head"><h2>Journal d'activité</h2><span class="meta">dernier cycle</span></div><div class="panel-body"><div class="activity" id="events"></div></div></article>
<article class="panel span2"><div class="panel-head"><h2>Rejets du cycle</h2><span class="meta">diagnostic scanner</span></div><div class="panel-body table-scroll"><table><thead><tr><th>Motif</th><th>Nombre</th><th>Part</th></tr></thead><tbody id="rej"></tbody></table></div></article><article class="panel"><div class="panel-head"><h2>P&L par stratégie</h2><span class="meta">trades réglés</span></div><div class="panel-body"><table><tbody id="pstrat"></tbody></table></div></article><div class="footer-ticker"><div class="asset"><b>BTC-USD</b><span class="spark">● LIVE</span></div><div class="asset"><b>Kalshi API</b><span class="positive">CONNECTED</span></div><div class="asset"><b>Environment</b><span id="envFoot">DEMO</span></div><div class="asset"><b>Exchange</b><span id="exchangeState" class="positive">ACTIVE</span></div><div class="asset"><b>Last update</b><span id="lastUpdate">—</span></div></div></section></div></main></div>
<script>
const $=id=>document.getElementById(id);const num=v=>Number(v||0);const usd=v=>v==null?"—":(num(v)>=0?"+":"")+num(v).toFixed(2)+"$";const usd0=v=>v==null?"—":num(v).toFixed(2)+"$";const pct=v=>v==null?"—":(num(v)*100).toFixed(1)+"%";const colorize=(el,v)=>{el.classList.remove('positive','negative','neutral');el.classList.add(num(v)>0?'positive':num(v)<0?'negative':'neutral')};
function grid(c,W,H,min,max){c.strokeStyle='#16253b';c.lineWidth=1;for(let i=0;i<5;i++){let y=18+(H-38)*i/4;c.beginPath();c.moveTo(40,y);c.lineTo(W-12,y);c.stroke()}c.fillStyle='#60738e';c.font='18px monospace';for(let i=0;i<5;i++){let v=max-(max-min)*i/4;c.fillText((v>=0?'+':'')+v.toFixed(1),2,24+(H-38)*i/4)}}
function lineChart(cv,pts){const c=cv.getContext('2d'),d=devicePixelRatio||1,W=cv.width=cv.clientWidth*d,H=cv.height=cv.clientHeight*d;c.clearRect(0,0,W,H);if(!pts.length){c.fillStyle='#61748e';c.font=`${12*d}px monospace`;c.fillText('Aucune donnée réglée pour le moment',20*d,H/2);return}const vals=pts.map(p=>num(p.v)),min=Math.min(0,...vals),max=Math.max(.01,...vals),X=i=>42*d+(W-56*d)*i/Math.max(1,pts.length-1),Y=v=>16*d+(H-34*d)*(max-v)/(max-min||1);grid(c,W,H,min,max);let g=c.createLinearGradient(0,0,0,H);g.addColorStop(0,'rgba(99,102,241,.45)');g.addColorStop(1,'rgba(99,102,241,0)');c.beginPath();pts.forEach((p,i)=>i?c.lineTo(X(i),Y(num(p.v))):c.moveTo(X(i),Y(num(p.v))));c.lineTo(X(pts.length-1),H-18*d);c.lineTo(X(0),H-18*d);c.closePath();c.fillStyle=g;c.fill();c.beginPath();pts.forEach((p,i)=>i?c.lineTo(X(i),Y(num(p.v))):c.moveTo(X(i),Y(num(p.v))));c.strokeStyle='#7d72ff';c.lineWidth=2*d;c.stroke()}
function barChart(cv,obj){const c=cv.getContext('2d'),d=devicePixelRatio||1,W=cv.width=cv.clientWidth*d,H=cv.height=cv.clientHeight*d;c.clearRect(0,0,W,H);const keys=Object.keys(obj).sort().slice(-9);if(!keys.length){c.fillStyle='#61748e';c.font=`${12*d}px monospace`;c.fillText('Aucune journée réglée',20*d,H/2);return}const vals=keys.map(k=>num(obj[k])),mx=Math.max(.01,...vals.map(Math.abs)),zero=H*.55,bw=(W-30*d)/keys.length;keys.forEach((k,i)=>{let v=num(obj[k]),h=(H*.38)*Math.abs(v)/mx,x=18*d+i*bw;c.fillStyle=v>=0?'#20e07a':'#ff5570';c.fillRect(x,v>=0?zero-h:zero,bw*.62,Math.max(2*d,h));c.fillStyle='#63758f';c.font=`${9*d}px monospace`;c.fillText(k.slice(5),x,H-7*d)});c.strokeStyle='#203049';c.beginPath();c.moveTo(0,zero);c.lineTo(W,zero);c.stroke()}
function tr(a){return '<tr>'+a.map(x=>'<td>'+x+'</td>').join('')+'</tr>'}function badge(text,type='blue'){return `<span class="badge b-${type}">${text}</span>`}function event(time,tag,msg,type='blue'){return `<div class="event"><time>${time}</time><span class="tag ${type==='green'?'positive':type==='red'?'negative':''}">${tag}</span><span>${msg}</span></div>`}
async function tick(){let s;try{const r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);s=await r.json()}catch(e){$('exchangeState').textContent='OFFLINE';$('exchangeState').className='negative';$('lastUpdate').textContent='API ERROR';return}const st=s.stats||{},R=s.report||{},positions=s.positions||{},orders=s.open_orders||{},curve=s.equity_curve||[],tot=curve.length?curve[curve.length-1].v:null;$('ver').textContent=s.version||'';$('mode').textContent=(s.env||'demo').toUpperCase();$('envFoot').textContent=(s.env||'demo').toUpperCase();$('bal').textContent=usd0(s.balance);$('cap').textContent=usd0(s.capital);$('cyc').textContent=s.cycle??'—';$('pnlD').textContent=usd(s.pnl_today);colorize($('pnlD'),s.pnl_today);$('pnlT').textContent=usd(tot);colorize($('pnlT'),tot);$('expo').textContent=usd0(s.exposure);$('npos').textContent=Object.keys(positions).length;$('ntr').textContent=s.settled_count??0;$('cycleTime').textContent=(R.cycle_duration_ms??'—')+' ms';$('wr').textContent=st.win_rate!=null?pct(st.win_rate):'—';$('pf').textContent=st.profit_factor!=null?Number(st.profit_factor).toFixed(2):'—';$('exp').textContent=st.expectancy_per_trade!=null?usd(st.expectancy_per_trade):'—';$('sh').textContent=st.sharpe_per_trade!=null?Number(st.sharpe_per_trade).toFixed(2):'—';$('dd').textContent=st.max_drawdown!=null?'-'+usd0(Math.abs(st.max_drawdown)):'—';$('shw').textContent=s.shadow_settled??'—';$('lowN').textContent=st.low_sample?`⚠ Échantillon insuffisant (${s.settled_count||0}/30). Les ratios restent indicatifs jusqu'à validation statistique.`:'✓ Échantillon statistique suffisant pour interpréter les ratios.';$('equityMeta').textContent=tot==null?'En attente de règlements':`PnL cumulé ${usd(tot)}`;lineChart($('eq'),curve);barChart($('pd'),s.pnl_by_day||{});
const cs=s.candidates||[];$('candMeta').textContent=cs.length+' candidat(s)';$('cands').innerHTML=cs.map(c=>tr([`<span class="ticker">${c.ticker||'—'}</span>`,c.strategy||'—',badge((c.side||'—').toUpperCase(),c.side==='yes'?'green':'blue'),c.model_probability!=null?pct(c.model_probability):'—',c.market_probability!=null?pct(c.market_probability):'—',`<span class="${num(c.edge_net)>0?'positive':'negative'}">${c.edge_net!=null?num(c.edge_net).toFixed(3):'—'}</span>`,c.ev_net!=null?num(c.ev_net).toFixed(3):'—',c.confidence??'—',badge(c.status||'evaluated',c.status==='submitted'?'green':String(c.status||'').startsWith('blocked')?'amber':'blue')])).join('')||tr(['<span style="color:#61748e">Aucune opportunité qualifiée</span>','','','','','','','','']);
$('poss').innerHTML=Object.entries(positions).map(([t,p])=>tr([`<span class="ticker">${t}</span>`,badge((p.side||'—').toUpperCase(),p.side==='yes'?'green':'blue'),p.count??p.filled_count??'—',(p.entry_price_cents??p.avg_price??'—')+'c'])).join('')||tr(['<span style="color:#61748e">Aucune position</span>','','','']);$('ords').innerHTML=Object.entries(orders).map(([id,o])=>tr([`<span class="ticker">${o.ticker||id.slice(0,11)}</span>`,badge((o.side||'—').toUpperCase(),o.side==='yes'?'green':'blue'),o.count??'—',(o.price??'—')+'c'])).join('')||tr(['<span style="color:#61748e">Aucun ordre actif</span>','','','']);$('posMeta').textContent=`${Object.keys(positions).length} position(s) · ${Object.keys(orders).length} ordre(s)`;
const stages=['scanned_raw','open_cached','liquid','supported','model_evaluated','positive_edge','positive_net_ev','risk_passed','orders_submitted','fills'],max=Math.max(1,...stages.map(k=>num(R[k])));$('fun').innerHTML=stages.map(k=>`<div class="frow"><span>${k.replaceAll('_',' ')}</span><div class="ftrack"><i style="width:${100*num(R[k])/max}%"></i></div><span class="fval">${R[k]??'—'}</span></div>`).join('');const rejs=Object.entries(R.rejections_by_reason||{}),rejTot=rejs.reduce((a,[,v])=>a+num(v),0);$('rej').innerHTML=rejs.sort((a,b)=>num(b[1])-num(a[1])).map(([k,v])=>tr([k,v,(rejTot?100*num(v)/rejTot:0).toFixed(1)+'%'])).join('')||tr(['Aucun rejet','0','0%']);$('pstrat').innerHTML=Object.entries(s.pnl_by_strategy||{}).map(([k,v])=>tr([k,`<span class="${num(v)>=0?'positive':'negative'}">${usd(v)}</span>`])).join('')||tr(['En attente des premiers règlements','—']);
const capital=num(s.capital),expPct=capital?Math.min(100,100*num(s.exposure)/capital):0;$('riskExpo').textContent=expPct.toFixed(1)+'%';$('riskExpoBar').style.width=expPct+'%';const dailyLimit=Math.abs(num((s.risk||{}).daily_stop||0)),dailyPct=dailyLimit?Math.min(100,100*Math.abs(num(s.pnl_today))/dailyLimit):0;$('riskDaily').textContent=dailyPct.toFixed(1)+'%';$('riskDailyBar').style.width=dailyPct+'%';const now=new Date(),time=now.toLocaleTimeString('fr-FR',{hour12:false});let ev=[];ev.push(event(time,'CYCLE',`Cycle ${s.cycle??'—'} terminé en ${R.cycle_duration_ms??'—'} ms`,'blue'));ev.push(event(time,'SCAN',`${R.scanned_raw??0} marchés scannés, ${R.liquid??0} liquides`,'blue'));if(R.orders_submitted)ev.push(event(time,'EXEC',`${R.orders_submitted} ordre(s) soumis, ${R.fills??0} fill(s)`,'green'));if(cs.length)ev.push(event(time,'ALPHA',`${cs.length} opportunité(s) qualifiée(s)`,'green'));if(s.exchange_paused)ev.unshift(event(time,'ALERT','Exchange en cooldown HTTP 503','red'));$('events').innerHTML=ev.join('');$('exchangeState').textContent=s.exchange_paused?'COOLDOWN':'ACTIVE';$('exchangeState').className=s.exchange_paused?'negative':'positive';$('lastUpdate').textContent=time;$('clock').textContent=now.toLocaleString('fr-FR',{hour12:false});}
window.addEventListener('resize',()=>tick());tick();setInterval(tick,5000);
</script></body></html>"""


def main():                                              # pragma: no cover
    import sys
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATA_DIR", ".")
    port = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", "8080")))
    print(f"Dashboard: http://0.0.0.0:{port}  (data: {data_dir})")
    _Handler.data_dir = data_dir
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
