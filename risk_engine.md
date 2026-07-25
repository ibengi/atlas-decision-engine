# Moteur de risque

Toutes les limites sont recalculées chaque cycle sur le **capital
effectif** `= min(solde broker, plafond configuré)`.

| Limite | Valeur défaut | Implémentation |
|---|---|---|
| Risque par trade | ≤ 1 % | PositionSizer (plafond dur) |
| Exposition ouverte totale | ≤ 5 % | RISK_BUDGET_PCT + open_risk |
| Stop de perte journalier | ≤ 5 % (min avec plafond $) | RiskManager.effective_daily_stop |
| Positions ouvertes | ≤ 3 | MAX_OPEN_POSITIONS |
| Pertes consécutives | arrêt à 3 | RiskManager.consecutive_losses |
| Risque par catégorie | ≤ 3 % | open_risk_by_category (vraie catégorie persistée) |
| Drawdown | throttle à 10 %, arrêt à 20 % | DD_THROTTLE / MAX_EQUITY_DRAWDOWN |
| Prix d'entrée | ≤ 85 c | MAX_ENTRY_CENTS |

Comportements notables :
- 1 % de 93,26 $ = 0,93 $ → un contrat à 95 c est **refusé** (0 contrat),
  le moteur ne force jamais un trade inabordable ;
- kill switch (`KILL_SWITCH=1`) prioritaire sur tout ;
- en production, l'absence de lecture du solde **bloque** le cycle (pas de
  repli sur un capital théorique).

Tests : `tests/test_sizing_small_account.py` (compte de 93,26 $).
