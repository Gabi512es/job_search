# 🎯 Job Scout — LinkedIn RSS + Claude API

Évalue automatiquement des offres LinkedIn via les flux RSS publics, sans scraping ni bot. Pas de compte LinkedIn requis, pas de risque de ban.

## Setup

```bash
# 1. Installer les dépendances
pip install -r requirements.txt

# 2. Configurer la clé API Anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Lancer
python job_scout.py
```

## Utilisation

```bash
# Run standard (nouveaux jobs seulement, max 30)
python job_scout.py

# Avec fetch de la description complète (meilleure précision, +lent)
python job_scout.py --fetch-full

# Voir seulement les OUI et OPPORTUNISTE
python job_scout.py --verdicts OUI,OPPORTUNISTE

# Max 50 jobs, output custom
python job_scout.py --max 50 --output ma_veille.json

# Réévaluer tous les jobs (ignore le cache)
python job_scout.py --no-cache
```

## Comment ça marche

```
LinkedIn RSS → feedparser → [optionnel: httpx fetch full desc] → Claude API → JSON verdict
```

- **RSS LinkedIn** : gratuit, public, no auth. Format: `linkedin.com/jobs/search/?keywords=...`
- **Cache** : `seen_jobs.json` évite de réévaluer les mêmes offres
- **Claude évalue** chaque offre selon ton profil Gabriel avec 4 verdicts :
  - ✅ `OUI` — forte correspondance, postuler maintenant
  - 🟡 `OPPORTUNISTE` — mérite une candidature adaptée
  - ❌ `NON` — passer
- **Output** : `results.json` cumulatif avec confidence, gaps, CV patches, suggestion cover letter

## Personnaliser les flux RSS

Dans `RSS_FEEDS` dans le script, modifier les URLs. Variables utiles :

| Paramètre | Valeur |
|-----------|--------|
| `f_TPR=r86400` | Dernières 24h |
| `f_TPR=r604800` | Dernière semaine |
| `f_WT=2` | Remote only |
| `f_JT=F` | Full-time only |
| `f_E=2` | Mid-Senior level |

**Exemple URL :**
```
https://www.linkedin.com/jobs/search/?keywords=AI+engineer+python&location=Europe&f_TPR=r604800&f_WT=2
```

## ⚠️ LinkedIn RSS : Workaround requis

LinkedIn a progressivement restreint ses flux RSS. Options pour les débloquer :

### ❌ Cookie de session (`li_at` / `JSESSIONID`) — ESSAYÉ, ABANDONNÉ. NE PAS RÉINTRODUIRE.

Cette approche a été implémentée puis retirée (audit du 09/09/2026). Ce qui a été
constaté dans le code avant suppression :

- Le header `Cookie: li_at=…; JSESSIONID=…` était injecté dans le dict `request_headers`
  **générique** de la boucle `parse_feeds`, donc envoyé à **toutes** les URLs de `RSS_FEEDS`.
- Or `RSS_FEEDS` ne contenait **aucune URL `linkedin.com`** : le cookie n'atteignait jamais
  LinkedIn et ne débloquait donc rien du tout.
- En revanche il divulguait un identifiant de session LinkedIn (= accès authentifié complet
  au compte) à 5 hôtes tiers sans aucun usage pour lui : weworkremotely.com, hnrss.org,
  arbeitnow.com, remotive.com, jobicy.com.
- Aucune détection d'expiration n'était possible : le check `✅` de la cellule Config
  testait seulement que la variable d'environnement était non vide.

Si un accès LinkedIn RSS authentifié redevient nécessaire un jour, c'est un chantier à part
entière : il faut de vraies URLs de flux `linkedin.com` **et** un header ciblé par hôte
(jamais un header global), pas la réintroduction de ce mécanisme. En attendant, LinkedIn
passe par Apify (voir Option C, c'est ce qui est en place).

### Option B — Sources RSS alternatives (sans login, toujours dispo)
Le script inclut déjà Remotive et We Work Remotely comme fallback. Tu peux aussi ajouter :
- `https://jobs.ashbyhq.com/<company>/rss` — AshbyHQ companies (startups AI)
- `https://boards.greenhouse.io/<company>/jobs.rss` — Greenhouse boards
- `https://api.ycombinator.com/v0.1/jobs.rss` — YC companies

### Option C — LinkedIn via Apify/RapidAPI
Si tu veux vraiment LinkedIn sans risque de ban, utilise une API commerciale :
- **Apify LinkedIn Jobs Scraper** (~$5/mois) → retourne JSON propre → brancher directement
- **RapidAPI LinkedIn Jobs** → endpoint REST → remplacer `parse_feeds()` par un appel API

## Structure du résultat JSON

```json
{
  "verdict": "OUI",
  "confidence": 8,
  "one_liner": "Agentic AI role at Series B startup, perfect stack match",
  "match_signals": ["RAG", "Python", "AI agents", "startup"],
  "gaps": ["No TypeScript mentioned in JD — minor"],
  "cv_patches": ["Highlight Amazon RAG chatbot", "Lead with AI Agents bullet"],
  "cover_letter": true,
  "cover_letter_angle": "Lead with 260h saved at Amazon + shipped RAG in prod",
  "red_flags": [],
  "title": "AI Engineer",
  "company": "Acme AI",
  "url": "https://linkedin.com/jobs/view/...",
  "evaluated_at": "2026-06-01T10:32:00"
}
```

## Automatisation (cron)

```bash
# Lancer chaque matin à 8h et appender les résultats
0 8 * * * cd /path/to/job_scout && python job_scout.py --fetch-full >> job_scout.log 2>&1
```
