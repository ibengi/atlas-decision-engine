# Image du moteur ATLAS.
#
# POURQUOI UN DOCKERFILE PLUTOT QUE LE BUILDER PAR DEFAUT
#   `model_gatekeeper.check_live_allowed()` — le dernier verrou avant
#   l'argent reel — lit `test_report.json` et `model_validation.json` par
#   chemin RELATIF, donc depuis le repertoire de travail du processus
#   (/app). `test_report.json` est genere par la suite de tests et reste
#   dans .gitignore : il ne peut PAS arriver par le depot. Absent, le
#   gatekeeper refuse le live — fail-closed, donc sans danger, mais le
#   verrou n'est jamais evaluable.
#
#   Le generer ICI, pendant le build, resout les deux problemes d'un coup:
#     * le rapport decrit le commit REELLEMENT deploye, pas une execution
#       locale d'il y a trois jours sur un autre arbre de travail ;
#     * une suite rouge casse le build, donc une image dont les tests
#       echouent n'existe jamais — au lieu d'exister avec un rapport qui
#       la contredit.
#
#   Un build reproductible est aussi la seule facon de PROUVER en CI ce que
#   l'image finale contient. C'est la raison principale de ce fichier.
#
# CE QUE CE FICHIER NE FAIT PAS
#   Il n'active rien. `model_validation.json` porte approved:false, aucune
#   variable de promotion n'est touchee, et rendre le gatekeeper evaluable
#   n'est pas le faire passer.

# Python 3.13 : version observee dans le conteneur en production
# (/app/.venv/lib/python3.13). Fixee pour que l'image ne derive pas.
ARG PYTHON_VERSION=3.13-slim

# ─────────────────────────────────────────────────────────────────────
# Etape 1 — TESTS. Produit test_report.json, et echoue si un test echoue.
# Les dependances de test vivent ICI et ne suivent pas dans l'image finale.
# ─────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION} AS tests
WORKDIR /src
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY . .

# Aucun reseau broker n'est joignable ni necessaire : la suite est
# entierement mockee. Un `run_tests.py` non nul arrete le build ici (RUN
# propage le code de sortie), donc une suite rouge ne produit pas d'image.
RUN python run_tests.py

# Garde-fou explicite : le rapport doit exister ET etre vert. Si
# run_tests.py changeait un jour de comportement (ecriture silencieuse
# d'un rapport rouge avec un code de sortie 0), le build s'arreterait
# quand meme ici plutot que d'expedier une image qui se contredit.
RUN python -c "\
import json, sys; \
r = json.load(open('test_report.json')); \
sys.exit(0) if (r.get('failures') == 0 and r.get('errors') == 0 \
                and r.get('ran', 0) > 0) else \
     sys.exit('test_report.json non vert: %r' % r)"

# ─────────────────────────────────────────────────────────────────────
# Etape 2 — IMAGE FINALE. Runtime seul : pas de pytest, pas de sources de
# test superflues cote execution.
# ─────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION} AS runtime
WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Les sources, dont model_validation.json (versionne, approved:false).
COPY . .

# Le rapport de tests vient de l'etape qui les a REELLEMENT executes.
COPY --from=tests /src/test_report.json /app/test_report.json

# Verification de l'image finale : les deux artefacts que le gatekeeper
# lit doivent exister au chemin exact ou il les cherche (le WORKDIR).
# Un COPY silencieusement casse fait echouer le build ici, pas six heures
# plus tard au moment de decider d'un passage LIVE.
RUN test -f /app/test_report.json || (echo "test_report.json absent de l'image" && exit 1)
RUN test -f /app/model_validation.json || (echo "model_validation.json absent de l'image" && exit 1)
# Controle de SCHEMA, pas de valeur. Le gatekeeper exige `approved is
# True` et un `generated_ts` numerique : un fichier ou l'un des deux
# manque ou porte le mauvais type se lit comme "non approuve", ce qui est
# la bonne issue mais pour la mauvaise raison — on ne saurait pas
# distinguer un modele refuse d'un fichier casse. On refuse donc ici.
# La VALEUR de `approved` n'est deliberement pas contrainte : la figer a
# false ferait echouer le build le jour d'une approbation legitime, cad
# exactement au pire moment.
RUN python -c "\
import json, sys; \
mv = json.load(open('/app/model_validation.json')); \
sys.exit(0) if (isinstance(mv.get('approved'), bool) \
                and isinstance(mv.get('generated_ts'), (int, float))) else \
     sys.exit('model_validation.json: schema invalide (approved=%r, '\
              'generated_ts=%r)' % (mv.get('approved'), mv.get('generated_ts')))"

# Identique au Procfile : DEMO, boucle. Aucun passage LIVE n'est cable ici.
CMD ["python", "kalshi_alpha_bot.py", "--loop", "--demo"]
