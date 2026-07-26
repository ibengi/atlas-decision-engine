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
"""
import json
import os
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:                                  # sys.path plat du depot (idempotent)
    from paths import add_src_to_path
    add_src_to_path()
except ImportError:
    pass

try:
    from performance import compute_stats
except Exception:                                    # pragma: no cover
    compute_stats = None


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
<title>KALSHI ALPHA ENGINE — tableau de bord</title>
<style>
:root{--bg:#0a0e17;--panel:#111826;--line:#1e2a3d;--tx:#d7e1f0;
--mut:#7787a0;--grn:#22c55e;--red:#ef4444;--blu:#6366f1;--amb:#f59e0b}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--tx);
font:13px/1.45 "JetBrains Mono","SFMono-Regular",Consolas,monospace}
header{display:flex;flex-wrap:wrap;gap:0;align-items:stretch;
border-bottom:1px solid var(--line);background:#0c1220}
.brand{padding:14px 20px;border-right:1px solid var(--line)}
.brand b{font-size:17px;letter-spacing:.5px}
.brand span{color:var(--blu)}
.kpi{padding:10px 18px;border-right:1px solid var(--line);min-width:118px}
.kpi .l{color:var(--mut);font-size:10px;text-transform:uppercase;
letter-spacing:.08em}
.kpi .v{font-size:16px;font-weight:700;margin-top:2px}
.grid{display:grid;gap:12px;padding:12px;
grid-template-columns:repeat(12,1fr)}
.card{background:var(--panel);border:1px solid var(--line);
border-radius:8px;padding:14px}
.card h2{font-size:11px;color:var(--mut);text-transform:uppercase;
letter-spacing:.1em;margin-bottom:10px}
.s12{grid-column:span 12}.s8{grid-column:span 8}.s6{grid-column:span 6}
.s4{grid-column:span 4}.s3{grid-column:span 3}
@media(max-width:1100px){.s8,.s6,.s4,.s3{grid-column:span 12}}
table{width:100%;border-collapse:collapse}
th{color:var(--mut);font-size:10px;text-transform:uppercase;
text-align:left;padding:4px 8px;border-bottom:1px solid var(--line)}
td{padding:5px 8px;border-bottom:1px solid #16202f;font-size:12px}
.pos{color:var(--grn)}.neg{color:var(--red)}.mut{color:var(--mut)}
.badge{display:inline-block;padding:1px 7px;border-radius:4px;
font-size:10px;font-weight:700}
.b-grn{background:#123524;color:var(--grn)}
.b-red{background:#3a1420;color:var(--red)}
.b-amb{background:#3a2a10;color:var(--amb)}
.metrics{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.m{background:#0d1421;border:1px solid var(--line);border-radius:6px;
padding:10px;text-align:center}
.m .l{color:var(--mut);font-size:9px;text-transform:uppercase}
.m .v{font-size:17px;font-weight:700;margin-top:3px}
canvas{width:100%;height:170px}
.note{color:var(--mut);font-size:11px;margin-top:8px}
footer{padding:8px 16px;color:var(--mut);font-size:11px;
border-top:1px solid var(--line)}
</style></head><body>
<header>
 <div class="brand"><b>KALSHI ALPHA<br>ENGINE <span id=ver>v11</span></b></div>
 <div class="kpi"><div class="l">Environnement</div>
   <div class="v"><span id=env class="badge b-grn">—</span></div></div>
 <div class="kpi"><div class="l">Solde</div><div class="v" id=bal>—</div></div>
 <div class="kpi"><div class="l">P&L jour</div><div class="v" id=pnlD>—</div></div>
 <div class="kpi"><div class="l">P&L total (réalisé)</div>
   <div class="v" id=pnlT>—</div></div>
 <div class="kpi"><div class="l">Exposition</div><div class="v" id=expo>—</div></div>
 <div class="kpi"><div class="l">Positions</div><div class="v" id=npos>—</div></div>
 <div class="kpi"><div class="l">Trades réglés</div><div class="v" id=ntr>—</div></div>
 <div class="kpi"><div class="l">Capital effectif</div><div class="v" id=cap>—</div></div>
 <div class="kpi"><div class="l">Cycle</div><div class="v" id=cyc>—</div></div>
</header>
<div class="grid">
 <div class="card s8"><h2>Performance du portefeuille
   (PnL réalisé cumulé)</h2><canvas id=eq height=170></canvas>
   <div class="note" id=eqNote></div></div>
 <div class="card s4"><h2>Métriques clés</h2>
   <div class="metrics">
    <div class=m><div class=l>Win rate</div><div class=v id=wr>—</div></div>
    <div class=m><div class=l>Profit factor</div><div class=v id=pf>—</div></div>
    <div class=m><div class=l>Expectancy</div><div class=v id=exp>—</div></div>
    <div class=m><div class=l>Sharpe/trade</div><div class=v id=sh>—</div></div>
    <div class=m><div class=l>Max drawdown</div><div class=v id=dd>—</div></div>
    <div class=m><div class=l>Shadow réglées</div><div class=v id=shw>—</div></div>
   </div><div class="note" id=lowN></div></div>
 <div class="card s8"><h2>Candidats du dernier cycle</h2>
   <table><thead><tr><th>Ticker</th><th>Stratégie</th><th>Côté</th>
   <th>Prob. modèle</th><th>Prob. marché</th><th>Edge net</th>
   <th>EV net</th><th>Conf.</th><th>Statut</th></tr></thead>
   <tbody id=cands></tbody></table>
   <div class="note" id=candNote></div></div>
 <div class="card s4"><h2>Positions ouvertes</h2>
   <table><thead><tr><th>Ticker</th><th>Côté</th><th>Taille</th>
   <th>Prix moy.</th></tr></thead><tbody id=poss></tbody></table>
   <h2 style="margin-top:14px">Ordres suivis</h2>
   <table><thead><tr><th>Ticker</th><th>Côté</th><th>Taille</th>
   <th>Prix</th></tr></thead><tbody id=ords></tbody></table></div>
 <div class="card s4"><h2>P&L par jour (réalisé)</h2>
   <canvas id=pd height=170></canvas></div>
 <div class="card s4"><h2>Funnel du cycle</h2><table><tbody id=fun></tbody>
   </table></div>
 <div class="card s4"><h2>Rejets du cycle / P&L par stratégie</h2>
   <table><tbody id=rej></tbody></table>
   <table style="margin-top:10px"><tbody id=pstrat></tbody></table></div>
</div>
<footer id=foot>chargement…</footer>
<script>
const $=id=>document.getElementById(id);
const usd=v=>v==null?"—":(v>=0?"+":"")+Number(v).toFixed(2)+"$";
const usd0=v=>v==null?"—":Number(v).toFixed(2)+"$";
const cls=v=>v>0?"pos":(v<0?"neg":"");
function line(cv,pts,color){const c=cv.getContext("2d"),W=cv.width=cv.clientWidth*2,
H=cv.height=340;c.clearRect(0,0,W,H);if(!pts.length){c.fillStyle="#7787a0";
c.font="22px monospace";c.fillText("aucun trade réglé pour l’instant",20,H/2);return}
const vs=pts.map(p=>p.v),mn=Math.min(0,...vs),mx=Math.max(0.01,...vs),
X=i=>28+(W-40)*i/Math.max(1,pts.length-1),Y=v=>H-24-(H-48)*(v-mn)/(mx-mn);
c.strokeStyle="#1e2a3d";c.beginPath();c.moveTo(28,Y(0));c.lineTo(W-12,Y(0));
c.stroke();c.strokeStyle=color;c.lineWidth=3;c.beginPath();
pts.forEach((p,i)=>i?c.lineTo(X(i),Y(p.v)):c.moveTo(X(i),Y(p.v)));c.stroke();
c.fillStyle=color+"22";c.lineTo(X(pts.length-1),Y(mn));c.lineTo(X(0),Y(mn));
c.closePath();c.fill();}
function bars(cv,obj){const c=cv.getContext("2d"),W=cv.width=cv.clientWidth*2,
H=cv.height=340;c.clearRect(0,0,W,H);const ks=Object.keys(obj).sort().slice(-10);
if(!ks.length){c.fillStyle="#7787a0";c.font="22px monospace";
c.fillText("aucune journée réglée",20,H/2);return}
const vs=ks.map(k=>obj[k]),mx=Math.max(0.01,...vs.map(Math.abs)),
bw=(W-40)/ks.length,Y0=H/2;ks.forEach((k,i)=>{const v=obj[k],
h=(H/2-30)*Math.abs(v)/mx;c.fillStyle=v>=0?"#22c55e":"#ef4444";
c.fillRect(24+i*bw,v>=0?Y0-h:Y0,bw*.7,Math.max(2,h));
c.fillStyle="#7787a0";c.font="16px monospace";
c.fillText(k.slice(5),24+i*bw,H-6);});
c.strokeStyle="#1e2a3d";c.beginPath();c.moveTo(0,Y0);c.lineTo(W,Y0);c.stroke();}
function row(cells){return "<tr>"+cells.map(c=>"<td>"+c+"</td>").join("")+"</tr>"}
async function tick(){let s;try{s=await(await fetch("/api/state")).json()}
catch(e){$("foot").textContent="API injoignable: "+e;return}
$("ver").textContent=s.version||"";
$("env").textContent=(s.env||"demo").toUpperCase();
$("env").className="badge "+((s.env||"demo")==="demo"?"b-grn":"b-red");
$("bal").textContent=usd0(s.balance);$("cap").textContent=usd0(s.capital);
$("cyc").textContent=s.cycle??"—";
$("pnlD").textContent=usd(s.pnl_today);$("pnlD").className="v "+cls(s.pnl_today);
const tot=(s.equity_curve.length? s.equity_curve[s.equity_curve.length-1].v:null);
$("pnlT").textContent=usd(tot);$("pnlT").className="v "+cls(tot);
$("expo").textContent=usd0(s.exposure);
$("npos").textContent=Object.keys(s.positions||{}).length;
$("ntr").textContent=s.settled_count;
const st=s.stats||{};const low=st.low_sample;
$("wr").textContent=st.win_rate!=null?(100*st.win_rate).toFixed(1)+"%":"—";
$("pf").textContent=st.profit_factor!=null?st.profit_factor:"—";
$("exp").textContent=st.expectancy_per_trade!=null?usd(st.expectancy_per_trade):"—";
$("sh").textContent=st.sharpe_per_trade!=null?st.sharpe_per_trade:"—";
$("dd").textContent=st.max_drawdown!=null?("-"+usd0(st.max_drawdown)):"—";
$("shw").textContent=s.shadow_settled??"—";
$("lowN").textContent=low?("⚠ échantillon insuffisant (n="+s.settled_count+
" < 30) : ratios non significatifs — affichage honnête, pas de chiffres gonflés."):"";
line($("eq"),s.equity_curve,"#6366f1");
$("eqNote").textContent=s.equity_curve.length?
("dernier point: "+usd(tot)+" sur "+s.settled_count+" trade(s) réglé(s)"):
"la courbe apparaîtra au premier trade réglé — aucune donnée simulée.";
bars($("pd"),s.pnl_by_day||{});
$("cands").innerHTML=(s.candidates||[]).map(c=>row([c.ticker,c.strategy||"—",
(c.side||"—").toUpperCase(),c.model_probability!=null?
(100*c.model_probability).toFixed(1)+"%":"—",
c.market_probability!=null?(100*c.market_probability).toFixed(1)+"%":"—",
"<span class="+(c.edge_net>0?"pos":"neg")+">"+
(c.edge_net!=null?c.edge_net.toFixed(3):"—")+"</span>",
c.ev_net!=null?c.ev_net.toFixed(3):"—",c.confidence??"—",
"<span class='badge "+(c.status==="submitted"?"b-grn":
(String(c.status||"").startsWith("blocked")?"b-amb":"b-red"))+"'>"+
(c.status||"évalué")+"</span>"])).join("")||
row(["<span class=mut>aucun candidat au dernier cycle</span>","","","","","","","",""]);
$("candNote").textContent="Probabilités du modèle btc15m-v1.0-ref NON calibré : "+
"à valider par le rapport Brier (shadow) avant toute confiance.";
$("poss").innerHTML=Object.entries(s.positions||{}).map(([t,p])=>row([t,
(p.side||"—").toUpperCase(),p.count??p.filled_count??"—",
(p.entry_price_cents??p.avg_price??"—")+"c"])).join("")||
row(["<span class=mut>aucune position locale</span>","","",""]);
$("ords").innerHTML=Object.entries(s.open_orders||{}).map(([id,o])=>row([
o.ticker||id.slice(0,10),(o.side||"—").toUpperCase(),o.count??"—",
(o.price??"—")+"c"])).join("")||
row(["<span class=mut>aucun ordre suivi</span>","","",""]);
const R=s.report||{};
$("fun").innerHTML=["scanned_raw","open_cached","liquid","supported",
"model_evaluated","positive_edge","positive_net_ev","risk_passed",
"orders_submitted","fills"].map(k=>{const f=(R.funnel_conversion||{})[k];
return row([k,(R[k]??"—"),f&&f.pct_of_prev!=null?f.pct_of_prev+"%":""])}).join("");
$("rej").innerHTML=Object.entries(R.rejections_by_reason||{}).map(([k,v])=>
row([k,v])).join("")||row(["<span class=mut>aucun rejet</span>",""]);
$("pstrat").innerHTML=Object.entries(s.pnl_by_strategy||{}).map(([k,v])=>
row([k,"<span class="+cls(v)+">"+usd(v)+"</span>"])).join("")||
row(["<span class=mut>P&L par stratégie : après premiers règlements</span>",""]);
$("foot").textContent="Dernière écriture moteur: "+(s.ts||"—")+
" · cycle_id "+((R.cycle_id)||"—")+" · durée "+(R.cycle_duration_ms??"—")+" ms"+
(s.exchange_paused?" · ⚠ exchange en cooldown 503":"");}
tick();setInterval(tick,5000);
</script></body></html>"""


def main():                                              # pragma: no cover
    import sys
    data_dir = sys.argv[1] if len(sys.argv) > 1 else \
        os.environ.get("DATA_DIR", ".")
    port = int(os.environ.get("PORT",
                              os.environ.get("DASHBOARD_PORT", "8080")))
    print(f"Dashboard: http://0.0.0.0:{port}  (data: {data_dir})")
    _Handler.data_dir = data_dir
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
