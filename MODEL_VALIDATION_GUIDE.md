# GUIDE DE VALIDATION DU MODELE BTC 15 MINUTES

## Statut actuel — a lire d'abord
Le modele livre est `btc15m-baseline-0.1` : une baseline ANALYTIQUE
(Phi(ln(S/K)/(sigma*sqrt(t)) + momentum borne), documentee dans
btc_probability_model.py) **NON VALIDEE**. Aucune rentabilite n'est
demontree ni pretendue. Le livrable est pret pour le SHADOW MODE uniquement.

## Donnees historiques qui MANQUENT encore pour valider
1. Predictions shadow REGLEES en conditions reelles : 0 aujourd'hui.
   Objectif minimal : GATE_MIN_PREDICTIONS=300 predictions reglees et
   GATE_MIN_TRADES=100 trades theoriques, soit environ 3 a 7 jours de
   shadow continu sur KXBTC15M (~96 fenetres/jour).
2. Historique de carnets Kalshi horodates (bid/ask reels au moment de la
   decision) : indispensable pour un backtest realiste ; le shadow store
   le construit automatiquement (section 4).
3. Prix CF Benchmarks RTI (source officielle de reglement Kalshi) : le
   contexte utilise Coinbase/Kraken/Bitstamp comme approximation ; l'ecart
   RTI/consensus doit etre mesure avant tout live.
4. Fills reels demo pour calibrer le proxy de slippage (actuellement un
   tampon constant SLIPPAGE_BUFFER_CENTS + UNCERTAINTY_BUFFER).
5. Des etiquettes de reglement KXBTCD FIABLES. Mesure le 2026-09-02 :
   62 evenements quotidiens clos avec BTC AU-DESSUS du strike (jusqu'a
   +12 %) portaient result="no" dans btc_daily_settlements.jsonl — l'API
   avait ete interrogee dans les minutes suivant la cloture et sa premiere
   reponse figee. Depuis, un reglement n'est accepte que sous statut
   finalise OU confirme par deux lectures identiques espacees de 30 min
   (btc_daily_evidence.SETTLE_MIN_LAG_S / SETTLE_CONFIRM_MIN_S). Les lignes
   anterieures a ce protocole sont conservees mais EXCLUES de
   calibration_records() par defaut. Verifier n'importe quel journal :
   ```
   python tools/daily_label_audit.py $DATA_DIR/btc_daily_settlements.jsonl <serie_spot.json>
   ```
   Sortie 0 seulement si aucune etiquette impossible n'est trouvee.

## Procedure
1) SHADOW : `SHADOW_MODE=1 python kalshi_alpha_bot.py --demo --btc --loop`
   (ou --shadow). Chaque candidat BTC est journalise dans
   shadow_predictions.json puis regle automatiquement.
2) BRIER HORS ECHANTILLON (la regle decisive, voir "Interpretation
   honnete" plus bas) :
   ```
   python tools/brier_oos.py $DATA_DIR/shadow_predictions.json
   ```
   Decoupe chronologique 60/20/20, modele et baseline marche (`yes_ask/100`)
   notes sur les MEMES lignes, seule la tranche TEST decide. Le rapport
   porte le SHA-256 du jeu de donnees. Sortie 0 seulement si PASS.

   Le seuil "nombre minimal de dates de reglement INDEPENDANTES" n'est pas
   defini dans ce depot (T7-I sect. 9 : "REQUIRES OPERATOR APPROVAL"). Tant
   qu'il ne l'est pas, le verdict plafonne a INDETERMINATE : l'outil ne peut
   pas se declarer valide lui-meme.

   NOTE : la version precedente de cette section appelait
   `ShadowPredictionStore().as_calibration_obs()` et `mc.save(...)`. Ni l'un
   ni l'autre n'existe dans ce depot — la procedure documentee n'etait pas
   executable, donc la regle decisive n'avait jamais pu etre verifiee.
3) BACKTEST : exporter les lignes du shadow store au format backtest puis
   `bt.save_report(bt.run_backtest(rows), "model_validation_report.json")`
   et y ajouter `"model_hash": model_gatekeeper.model_hash()`.
4) GATEKEEPER : `python -c "import model_gatekeeper as g; print(g.check_live_allowed())"`
   Le live reste bloque tant que la liste des criteres echoues n'est pas vide.
5) LIVE (jamais automatique) : exige KALSHI_ENV_CONFIRM=LIVE,
   LIVE_TRADING_CONFIRMED=YES, LIVE_TRADING=1, MODEL_APPROVED_FOR_LIVE=YES
   ET un rapport de validation recent accepte par le gatekeeper.

## Modele QUOTIDIEN (KXBTCD) — a ne pas confondre avec le 15 min
Le modele btc15m-v1.0-ref extrapole une vol 1 min (29 rendements) par
sqrt(T) jusqu'a ~900 min : vol 24 h implicite ~1,6 % contre 2,5-4 %
realisee, terme de momentum sature a la borne dans 97 % des cas
quotidiens, penalite d'horizon inactive sous 1440 min. Il est
surconfiant sur chaque strike quotidien. Un modele quotidien separe se
valide avec :
   ```
   python tools/daily_research.py $DATA_DIR/btc_daily_predictions.jsonl $DATA_DIR/btc_daily_settlements.jsonl
   ```
(une observation par evenement et par checkpoint, etiquettes confirmees
seulement, strikes spot_proxy exclus, arret si l'etiquette est degeneree,
tranches spread<=4/6/10 et pipeline-accepted rapportees separement). Les
estimateurs de volatilite candidats sont dans tools/daily_vol.py ; ils ne
sont valides que sur GBM synthetique tant qu'aucun historique de bougies
reel n'est accessible.

## Interpretation honnete
- Brier du modele DOIT battre le baseline marche (yes_ask/100) hors
  echantillon ; sinon le marche predit mieux que le modele et il n'y a
  aucun edge a exploiter.
- Un PnL net positif sur < 100 trades n'est PAS concluant.
- La calibration par bins sous-apprend volontairement (10 bins, lissage) ;
  ne pas l'affiner tant que n < 500.
