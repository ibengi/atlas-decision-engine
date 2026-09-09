"""
model_gatekeeper.py — v3 (audit finding A08)
Verrou final avant tout trading LIVE. Regle I du cahier des charges :
tant que les tests ne passent pas, le programme reste en
DEMO + NO_LIVE_PROMOTION. LIVE n'est JAMAIS active automatiquement.

check_live_allowed() -> (bool, [criteres echoues])
Criteres TOUS obligatoires :
  1. NO_LIVE_PROMOTION != 1 (defaut : 1, donc live bloque par defaut) ;
  2. MODEL_APPROVED_FOR_LIVE=YES (approbation humaine explicite) ;
  3. test_report.json : schema STRICT, tests_run > 0, tests_passed ==
     tests_run, horodatage fini/plausible, de moins de 7 jours, et LIE
     au code qui tourne (`code_identity`) ;
  4. model_validation.json : schema STRICT, approved=true, moins de 30
     jours, horodatage coherent avec celui des tests, et lie a la meme
     evidence que le rapport de tests (`model_validation_sha256`).

Ce que l'audit a trouve, et pourquoi la version precedente l'acceptait
(``check_live_allowed`` v2-ref) :

  * ``ran`` n'etait pas lu du tout. Un rapport annoncant ZERO test
    execute passait le critere « tests verts » : zero echec sur zero
    test est vrai et ne prouve rien.
  * ``float(NaN)`` ne leve pas. ``(time.time() - nan) / 86400 > 7`` est
    False, donc un horodatage NaN se lisait comme « frais ».
  * Rien ne bornait le futur. Un rapport date de dix jours en avant
    donnait un age negatif, donc « frais » aussi.
  * ``tr.get("failures", 1) != 0`` traitait un champ ABSENT comme un
    defaut, et un champ present mais non entier (``"0"``, ``None``,
    ``[]``) comme une valeur a comparer.
  * Aucun lien entre les artefacts et le code/modele evalue : un
    rapport vert produit par un autre arbre restait acceptable.

Principe applique partout ici : une valeur manquante, non finie, du
mauvais type ou hors domaine est un REFUS, jamais un defaut. Le verrou
echoue ferme.
"""

import hashlib
import json
import math
import os
import time

#: Fenetres de fraicheur (jours).
MAX_TEST_REPORT_AGE_DAYS = 7.0
MAX_MODEL_VALIDATION_AGE_DAYS = 30.0
#: Tolerance d'horloge acceptee vers le futur (secondes). Au-dela, un
#: artefact date en avant n'est pas « frais », il est incoherent.
MAX_FUTURE_SKEW_S = 300.0
#: L'evidence modele ne peut pas etre plus RECENTE que les tests qui
#: l'accompagnent : elle est censee decrire le modele que ces tests ont
#: exerce, et la liaison `model_validation_sha256` prouve deja qu'elle
#: etait presente, octet pour octet, au moment de leur execution. Un
#: manifeste date apres eux est donc incoherent, pas simplement recent.
MAX_MODEL_AHEAD_OF_TESTS_S = MAX_FUTURE_SKEW_S


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _finite_number(value):
    """(ok, value). NaN et Infinity sont explicitement hors domaine : ils
    traversent silencieusement toute comparaison d'age."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, None
    v = float(value)
    if not math.isfinite(v):
        return False, None
    return True, v


def _non_negative_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return False, None
    return True, value


def code_identity(root: str = None) -> str:
    """Empreinte du code de production reellement present.

    sha256 des couples (nom de fichier, sha256 du contenu) de tous les
    modules Python a la racine du depot, tries. Les repertoires `tests/`,
    `tools/` et `research/` en sont exclus : ils ne sont pas charges par
    le runtime et leur presence varie selon l'etape d'image.

    Un rapport de tests qui ne porte pas CETTE empreinte a ete produit
    par un autre arbre : vert ou non, il ne dit rien du code qui tourne.
    """
    root = root or os.path.dirname(os.path.abspath(__file__))
    parts = []
    try:
        names = sorted(n for n in os.listdir(root) if n.endswith(".py"))
    except OSError:
        return ""
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                parts.append(name + ":" + hashlib.sha256(fh.read()).hexdigest())
        except OSError:
            return ""
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def file_sha256(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _check_test_report(now: float, failed: list):
    """-> le timestamp du rapport, ou None si le rapport est refuse."""
    tr = _load("test_report.json")
    if tr is None:
        failed.append("test_report.json absent ou illisible")
        return None
    if not isinstance(tr, dict):
        failed.append(f"test_report.json: objet attendu, "
                      f"{type(tr).__name__} trouve")
        return None
    ok_ran, ran = _non_negative_int(tr.get("ran"))
    if not ok_ran:
        failed.append(f"test_report.json: 'ran' = {tr.get('ran')!r} n'est pas "
                      f"un nombre de tests")
        return None
    if ran == 0:
        failed.append("test_report.json: ZERO test execute -- zero echec sur "
                      "zero test ne prouve rien")
        return None
    counts = {}
    for field in ("failures", "errors", "skipped"):
        ok_f, value = _non_negative_int(tr.get(field))
        if not ok_f:
            failed.append(f"test_report.json: '{field}' = {tr.get(field)!r} "
                          f"n'est pas un entier positif ou nul")
            return None
        counts[field] = value
    passed = ran - counts["failures"] - counts["errors"] - counts["skipped"]
    if passed != ran:
        failed.append(f"tests non verts: {passed}/{ran} passes "
                      f"(failures={counts['failures']}, "
                      f"errors={counts['errors']}, "
                      f"skipped={counts['skipped']})")
    ok_ts, ts = _finite_number(tr.get("generated_ts"))
    if not ok_ts:
        failed.append(f"test_report.json: 'generated_ts' = "
                      f"{tr.get('generated_ts')!r} n'est pas un nombre fini")
        return None
    if ts > now + MAX_FUTURE_SKEW_S:
        failed.append(f"test_report.json date dans le futur "
                      f"({(ts - now) / 86400:.1f} j) -- horodatage incoherent")
        return None
    age_d = (now - ts) / 86400.0
    if age_d > MAX_TEST_REPORT_AGE_DAYS:
        failed.append(f"test_report.json trop ancien ({age_d:.1f} j)")
    want = code_identity()
    got = tr.get("code_identity")
    if not want:
        failed.append("empreinte du code de production incalculable -- "
                      "liaison artefact/code impossible a verifier")
    elif not isinstance(got, str) or len(got) != 64:
        failed.append("test_report.json: 'code_identity' absent ou invalide "
                      "-- rapport non lie au code evalue")
    elif got != want:
        failed.append(f"test_report.json produit par un AUTRE arbre "
                      f"(code_identity {got[:12]} != {want[:12]})")
    return ts


def _check_model_validation(now: float, test_ts, failed: list) -> None:
    mv = _load("model_validation.json")
    if mv is None:
        failed.append("model_validation.json absent ou illisible")
        return
    if not isinstance(mv, dict):
        failed.append(f"model_validation.json: objet attendu, "
                      f"{type(mv).__name__} trouve")
        return
    if mv.get("approved") is not True:
        failed.append(f"model_validation.json non approuve "
                      f"(approved={mv.get('approved')!r})")
    version = mv.get("model_version")
    if not isinstance(version, str) or not version.strip():
        failed.append("model_validation.json: 'model_version' absent ou vide "
                      "-- evidence non rattachable a un modele")
    criteria = mv.get("criteria")
    if criteria is not None:
        if not isinstance(criteria, list):
            failed.append("model_validation.json: 'criteria' n'est pas une liste")
        else:
            unmet = [c.get("name") for c in criteria
                     if not isinstance(c, dict) or c.get("passed") is not True]
            if unmet:
                failed.append(f"criteres de validation non satisfaits: {unmet}")
    ok_ts, ts = _finite_number(mv.get("generated_ts"))
    if not ok_ts:
        failed.append(f"model_validation.json: 'generated_ts' = "
                      f"{mv.get('generated_ts')!r} n'est pas un nombre fini")
        return
    if ts > now + MAX_FUTURE_SKEW_S:
        failed.append(f"model_validation.json date dans le futur "
                      f"({(ts - now) / 86400:.1f} j) -- horodatage incoherent")
        return
    age_d = (now - ts) / 86400.0
    if age_d > MAX_MODEL_VALIDATION_AGE_DAYS:
        failed.append(f"validation modele trop ancienne ({age_d:.0f} j)")
    if test_ts is not None and ts > test_ts + MAX_MODEL_AHEAD_OF_TESTS_S:
        failed.append(f"evidence modele posterieure aux tests de "
                      f"{(ts - test_ts) / 3600:.2f} h -- les tests n'ont pas "
                      f"exerce ce modele")


def _check_artifact_binding(failed: list) -> None:
    """Les deux artefacts doivent decrire la MEME evidence : run_tests.py
    grave le sha256 du manifeste modele present au moment des tests."""
    tr = _load("test_report.json")
    if not isinstance(tr, dict):
        return
    declared = tr.get("model_validation_sha256")
    actual = file_sha256("model_validation.json")
    if not isinstance(declared, str) or len(declared) != 64:
        failed.append("test_report.json: 'model_validation_sha256' absent ou "
                      "invalide -- tests non lies au manifeste modele")
    elif not actual:
        failed.append("model_validation.json illisible pour la liaison")
    elif declared != actual:
        failed.append(f"model_validation.json a change depuis les tests "
                      f"({declared[:12]} != {actual[:12]}) -- artefact perime")


def check_live_allowed():
    failed = []
    now = time.time()
    if os.getenv("NO_LIVE_PROMOTION", "1").strip() == "1":
        failed.append("NO_LIVE_PROMOTION=1 (defaut) : promotion live "
                      "interdite tant que non levee explicitement")
    if os.getenv("MODEL_APPROVED_FOR_LIVE", "") != "YES":
        failed.append("MODEL_APPROVED_FOR_LIVE=YES absent")
    test_ts = _check_test_report(now, failed)
    _check_model_validation(now, test_ts, failed)
    _check_artifact_binding(failed)
    return (len(failed) == 0), failed
