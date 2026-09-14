# Architecture — Job Scout multi-utilisateurs

> Document de référence. Décrit la transformation de deux notebooks
> mono-utilisateur en un moteur backend réutilisable, appelable depuis une
> application web (frontend Lovable + Supabase).
>
> **Statut** : les 6 blocs sont implémentés et validés sur un run réel.
> Schéma Supabase + `SupabaseStore` écrits, en attente d'un projet réel.
> **Antériorité** : issu de l'audit du code existant (même date), dont les
> constats sont repris ici sans être recopiés intégralement.

## Sommaire

- [0. Choix techniques](#0-choix-techniques)
- [1. Structure de fichiers](#1-structure-de-fichiers)
- [2. Schéma de profil utilisateur](#2-schéma-de-profil-utilisateur)
- [3. Interface commune des connecteurs](#3-interface-commune-des-connecteurs)
- [4. Moteur de scoring paramétré](#4-moteur-de-scoring-paramétré)
- [5. État et persistance](#5-état-et-persistance)
- [6. Export et génération de texte](#6-export-et-génération-de-texte)
- [7. Découpage d'implémentation](#7-découpage-dimplémentation)
- [8. Destination des résultats : `job_results`, pas Excel](#8-destination-des-résultats--job_results-pas-excel)
- [Annexe A — Corrections apportées à l'existant](#annexe-a--corrections-apportées-à-lexistant)

---

## Contexte et contraintes

Point de départ : deux notebooks Jupyter.

- `job_scout.ipynb` — profil Gabriel. Scraping LinkedIn via Apify + RSS
  multi-source, scoring Claude Haiku, export Excel, génération de lettres de
  motivation via Sonnet.
- `job_scout_camila/job_scout_camila.ipynb` — profil Camila, dérivé du premier.
  Scraping Xarxanet (BeautifulSoup) et InfoJobs via Apify, critères de scoring
  différents, génération d'emails courts.

Objectif : **généraliser ce qui est hardcodé**, pas réécrire from scratch.

Contraintes permanentes :

- Code et commentaires en anglais. Ce document et les README sont en français.
- Simplicité et lisibilité avant élégance architecturale. Pas d'injection de
  dépendances, pas de couches d'abstraction inutiles.
- **Aucune donnée personnelle réelle dans le code versionné.** Tout passe par
  des fichiers de config non versionnés ou des variables d'environnement.
- **Pas de conception d'API à ce stade.** On ne sait pas encore comment Lovable
  appellera le moteur (edge function Supabase, job asynchrone, autre). Le moteur
  doit juste être *appelable proprement plus tard* : fonctions claires,
  arguments explicites, pas d'état global, retours structurés — comme
  `cost_guard.py` le fait déjà avec son statut
  `OK` / `NEEDS_CONFIRMATION` / `REJECTED_OVER_HARD_CAP`.

---

## 0. Choix techniques

| Choix | Décision | Justification |
|---|---|---|
| **Pydantic v2** plutôt que dataclasses | Pydantic | Le profil viendra d'un `jsonb` Supabase, donc de l'extérieur. Pydantic valide au chargement et donne des messages lisibles (« `weights` ne somme pas à 1.0 ») au lieu d'un `KeyError` 40 lignes plus loin. Round-trip JSON gratuit (`model_validate` / `model_dump`) : c'est exactement l'interface avec Supabase, et plus tard avec un formulaire Lovable. Avec des dataclasses il faudrait écrire toute la validation à la main. |
| **Un package `jobscout/`** plutôt que des fichiers à plat | package | 4 connecteurs, qui vont grossir. Mais volontairement plat à l'intérieur : pas de `core/`, pas de `domain/`. |
| **Verdicts internes en anglais** `YES` / `MAYBE` / `NO` | renommer | L'existant avait l'interne en français (`OUI`) et Camila re-traduisait en espagnol à l'écriture, avec un commentaire de 4 lignes pour prévenir la confusion. Neutraliser l'interne supprime le problème : chaque profil déclare ses libellés d'affichage. |
| **Profil stocké en `jsonb`** plutôt qu'éclaté en 15 tables | jsonb | Un profil est un document : lu en entier, écrit en entier, jamais requêté par morceaux. Colle au modèle Pydantic 1:1. |
| **Une seule décision de coût par run** | oui | Le garde-fou agrège les plans de tous les connecteurs actifs et rend un verdict unique, avec le détail par connecteur. L'utilisateur voit un total, pas quatre demandes de confirmation. |
| **Run atomique** | oui | Tout le run attend le feu vert du garde-fou, y compris les sources gratuites. On ne veut pas d'un résultat à moitié rempli. Simplicité d'abord. |

---

## 1. Structure de fichiers

```
jobscout/
  profile.py          # modèles Pydantic du profil (section 2)          [FAIT]
  jobs.py             # JobPosting + identité (job_key)              [FAIT]
  cost_guard.py       # garde-fou de coût Apify (à la racine aujourd'hui) [FAIT]
  filters.py          # normalisation, dédup, règles d'exclusion     [FAIT]
  scoring.py          # moteur de scoring paramétré (section 4)       [FAIT]
  prompts.py          # construction des prompts depuis le profil     [FAIT]
  writing.py          # lettre/email : un seul mécanisme (section 6)
  collect.py          # plan -> garde-fou -> fetch (section 3.1)       [FAIT]
  pipeline.py         # run() : orchestration, zéro état global      [FAIT]
  connectors/                                                         [FAIT]
    base.py           # Protocol SourceConnector, Secrets, DumpStore
    rss.py
    linkedin_apify.py
    infojobs_apify.py
    xarxanet.py
  export/                                                             [FAIT]
    excel.py          # export secondaire (voir section 8)
    computed.py       # registre des valeurs dérivées
  store/                                                              [FAIT]
    base.py           # Protocol Store + JobResult / Application / RunRecord
    json_store.py     # développement : fichiers locaux
    supabase_store.py # implémenté                                   [FAIT]
tools/
  build_profiles.py   # adaptateur notebooks -> profiles/*.json          [FAIT]
profiles/             # GITIGNORÉ sauf example.json et README.md         [FAIT]
  gabriel.json
  camila.json
  example.json        # fictif, versionné, documente le schéma
  cv/gabriel.txt
  cv/camila.txt
tests/
  legacy_values.py             # extrait les valeurs des notebooks       [FAIT]
  test_profile_completeness.py # prouve qu'aucune valeur n'est perdue    [FAIT]
  test_filters.py              # différentiel contre les notebooks       [FAIT]
  test_connectors.py           # 4 connecteurs + garde-fou câblé         [FAIT]
  test_scoring.py              # différentiel sur 999 lignes réelles     [FAIT]
  test_store_export.py         # store, mapping job_results, export      [FAIT]
  test_pipeline.py             # run() complet, client factice           [FAIT]
  test_supabase_store.py       # SQL + logique, client factice           [FAIT]
supabase/
  schema.sql          # 6 tables + RLS, à appliquer une fois   [FAIT]
legacy/               # GITIGNORÉ — notebooks d'origine figés (référence)
dumps/                # GITIGNORÉ — payloads Apify bruts, rejouables     [FAIT]
```

Le CV n'est **jamais** dans le JSON de profil : celui-ci contient un `cv_path`
(ou, plus tard, une colonne `cv_text` en base). C'est ce qui règle les trois
fuites de données personnelles relevées à l'audit.

---

## 2. Schéma de profil utilisateur

Implémenté dans `jobscout/profile.py`. Six familles de champs, correspondant aux
six blocs identifiés comme spécifiques à une personne.

```python
class ScoringDimension(BaseModel):
    key: str          # "tech_fit"
    label: str        # "Tech" -> utilisé dans le breakdown affiché
    weight: float     # 0.40
    rubric: str       # le barème 0-10, injecté tel quel dans le prompt

class LlmFlag(BaseModel):
    """Booléen demandé au LLM, avec son ajustement déterministe."""
    key: str                  # "catalan_imprescindible"
    definition: str           # quand le mettre à true, injecté dans le prompt
    adjustment: float         # -3.0
    note: str                 # texte ajouté aux red_flags / match_signals
    target: Literal["red_flags", "match_signals"] = "red_flags"

class KeywordPenalty(BaseModel):
    """Ajustement calculé en Python, jamais demandé au LLM."""
    key: str                  # "corredor_maresme_r1"
    keywords: list[str]
    fields: list[str] = ["location", "title"]
    adjustment: float
    note: str

class ExclusionRule(BaseModel):
    kind: Literal["any_keyword", "keyword_unless", "location_not_in"]
    fields: list[str] = ["title", "company"]
    keywords: list[str] = []
    unless_keywords: list[str] = []   # kind="keyword_unless"
    allow: list[str] = []             # kind="location_not_in"
    deny: list[str] = []

class Thresholds(BaseModel):
    yes_above: float = 7.0    # YES   si score >  yes_above
    maybe_above: float = 5.0  # MAYBE si score >= maybe_above
```

Les trois `kind` d'`ExclusionRule` ont été choisis pour couvrir exactement les
quatre règles qui existent réellement, sans en inventer d'autres :

| `kind` | Couvre |
|---|---|
| `any_keyword` | Les stages/bootcamps de Gabriel ; les `becario`/`prácticas` de Camila |
| `keyword_unless` | Le `voluntariado` de Camila, exclu **sauf si** `contrato` apparaît |
| `location_not_in` | Les villes hors Barcelone de Camila |

Le profil complet :

```python
class UserProfile(BaseModel):
    profile_id: str
    display_name: str
    language: Literal["en", "es", "fr", "ca"] = "en"

    cv_path: str | None = None    # fichier hors git (développement)
    cv_text: str | None = None    # colonne Supabase (cible)
    candidate_summary: str = ""

    dimensions: list[ScoringDimension]
    thresholds: Thresholds = Thresholds()
    verdict_labels: VerdictLabels = VerdictLabels()
    llm_flags: list[LlmFlag] = []
    keyword_penalties: list[KeywordPenalty] = []
    exclusions: list[ExclusionRule] = []
    extra_output_fields: list[str] = []
    target_salary_rule: TargetSalaryRule | None = None

    sources: list[SourceConfig]            # union discriminée, section 3
    writing: WritingConfig | None = None   # section 6
    export: ExportConfig                   # section 6
    cost_policy: CostPolicy = CostPolicy() # réutilise cost_guard.py
```

### 2.1 Validation

Le schéma refuse activement les profils incohérents (chaque règle est couverte
par un test) :

- les poids des dimensions doivent sommer à 1.0 ;
- pas de clé dupliquée parmi `dimensions`, `llm_flags`, `keyword_penalties`, `sources` ;
- `maybe_above` ne peut pas dépasser `yes_above` ;
- une source `enabled: true` doit avoir sa configuration (mots-clés, localisation…) ;
- une colonne d'export doit être préfixée `field:`, `computed:` ou `flag:` ;
- `cv_path` et `cv_text` sont mutuellement exclusifs ;
- un CV introuvable lève une erreur plutôt que de scorer contre un CV vide.

### 2.2 Les deux profils réels

Générés par `python3 tools/build_profiles.py`, qui **lit** les valeurs dans les
notebooks au lieu de les retaper. Résumé de ce qui est capturé :

| | Gabriel | Camila |
|---|---|---|
| Langue des prompts | `en` | `es` |
| Dimensions / poids | `tech_fit` 0.40, `location` 0.25, `company_size` 0.20, `seniority` 0.15 | `perfil_fit` 0.45, `location` 0.25, `entidad_fit` 0.20, `condiciones` 0.10 |
| Seuils | 7.0 / 5.0 | 7.0 / 5.0 |
| Libellés | OUI / OPPORTUNISTE / NON | SÍ / OPORTUNISTA / NO |
| `llm_flags` | 0 | 5 (−3.0, −2.5, −1.5, −3.0, +1.0) |
| `keyword_penalties` | 0 | 1 — `corredor_maresme_r1`, 25 communes, −3.0 |
| `exclusions` | 1 règle, 16 mots-clés | 3 règles (5 mots-clés, 4 `voluntariado`, 8 villes) |
| Sources actives | `rss` (4 flux), `linkedin_apify` (6 mots-clés) | `xarxanet` (13 filtres), `infojobs_apify` (9 mots-clés) |
| Sources désactivées | `infojobs_apify`, `xarxanet` | `rss`, `linkedin_apify` |
| Rédaction | lettre, 550 mots min, anglais | email, 150 mots max, espagnol |
| Colonnes d'export | 16 + 9 (Tracker) | 14 + 11 (Tracker) |
| `target_salary_rule` | présent | `null` |

L'activation par utilisateur satisfait l'exigence de départ : un utilisateur en
France n'active pas les connecteurs Espagne-only.

### 2.3 Ce qui ne rentre pas proprement

Quatre points, tranchés et validés :

1. **`target_salary_rule`** — ce sont les prétentions salariales du candidat,
   avec une heuristique sur le titre et le sous-score de séniorité. Ça ne se
   généralise pas élégamment. Conservé comme champ optionnel **délibérément
   étroit**, plutôt que déguisé en mécanisme général. `null` chez Camila.

2. **Le booléen `cover_letter` demandé au LLM chez Gabriel était du code mort.**
   Il était bien demandé dans le prompt et écrit en Excel, mais la génération
   était déclenchée uniquement par `if oui:` — la valeur n'était jamais lue.
   Supprimé du schéma de sortie. La génération est gouvernée par
   `writing.generate_for_verdicts`.

3. **`TRACKER_XLSX = Path("job_tracker.xlsx")`** — déclaré une fois, utilisé
   zéro fois. Constante morte, supprimée.

4. **`FETCH_FULL` / `fetch_job_description`** — ce n'est pas un trait de profil
   mais une option de run. Déplacé dans `RunOptions` (section 5.1).

---

## 3. Interface commune des connecteurs

Le contrat de données, formalisé — il existait déjà implicitement, les quatre
connecteurs convergeaient vers cette forme sans que ce soit écrit nulle part :

```python
class JobPosting(BaseModel):
    id: str            # empreinte stable, cf. section 5.2
    title: str
    company: str = ""
    url: str
    summary: str = ""
    source: str        # "linkedin", "xarxanet", "infojobs", "weworkremotely.com"
    published: str = ""
    location: str = "" # généralisé : n'était rempli que par Xarxanet et InfoJobs
    raw: dict = {}     # payload d'origine, pour rejouer un mapping sans re-scraper
```

Généraliser `location` permet aux règles `location_not_in` et aux pénalités
géographiques de fonctionner pour tous les profils.

```python
class SourceConnector(Protocol):
    type: str

    def plan(self, cfg: SourceConfig) -> list[ConnectorPlan]:
        """Ce que le connecteur COMPTE faire, sans rien dépenser.
        Retourne [] pour les sources gratuites (RSS, Xarxanet)."""

    def fetch(self, cfg: SourceConfig, secrets: Secrets) -> list[JobPosting]:
        """Exécute. Ne doit être appelé qu'après un feu vert du garde-fou."""
```

Deux méthodes, aucun héritage. Ajouter une source = un fichier dans
`connectors/` avec ces deux méthodes.

### 3.1 Articulation avec `cost_guard.py`

```python
# 1. Chaque connecteur actif déclare son intention. Aucun appel réseau.
plans = [p for c in active_connectors for p in c.plan(cfg_of(c))]
#    connecteurs gratuits : []
#    infojobs_apify       : [plan_infojobs(n_keywords=9, max_per_search=12)]
#    linkedin_apify       : [plan_linkedin(n_keywords=6, n_work_types=2, max_per_search=100)]

# 2. UNE décision pour tout le run.
estimate = check_run_cost(plans, profile.cost_policy,
                          confirmed_fingerprint=opts.confirmed_fingerprint)

# 3. Rien de payant sans feu vert. Run atomique : les sources gratuites
#    attendent aussi.
if not estimate.may_run:
    return RunReport(status=estimate.decision, cost=estimate, results=[])
    #  NEEDS_CONFIRMATION     -> l'appelant montre estimate.to_dict(),
    #                            revient avec estimate.fingerprint
    #  REJECTED_OVER_HARD_CAP -> terminé, rien à négocier

# 4. Feu vert : on fetch.
jobs = [j for c in active_connectors for j in c.fetch(cfg_of(c), secrets)]
```

**Qui fait quoi** : le connecteur déclare (`plan`), le pipeline décide
(`check_run_cost`), le connecteur exécute (`fetch`). Un connecteur ne connaît pas
le garde-fou et ne peut pas le contourner : il ne dépense que dans `fetch`, et
`fetch` n'est atteint qu'après le contrôle.

**Le `fingerprint`** identifie le plan exact + la policy + le coût. Une
confirmation n'est valide que pour le plan pour lequel elle a été émise : un
utilisateur ne peut pas approuver un run à \$0,50 et voir un run à \$4,00
s'exécuter sous cette approbation. Une confirmation ne peut jamais transformer
`REJECTED_OVER_HARD_CAP` en `OK`.

**Le dump brut** — aujourd'hui spécifique à InfoJobs — devient une capacité de
tout connecteur Apify (`reuse_dump: true`). Dans ce mode `plan()` retourne `[]`,
puisque rejouer un dump déjà payé ne coûte rien.

### 3.3 Deux changements de comportement assumés (bloc 2)

L'unification des filtres modifie volontairement deux comportements. Les deux
sont couverts par un test qui les affirme explicitement.

**1. `normalize_text` remplace la ponctuation par une espace au lieu de la
supprimer.** L'ancien `_norm` de Gabriel transformait `"Senior/Lead Engineer"`
en `"seniorlead engineer"` ; la version retenue (celle de Camila) donne
`"senior lead engineer"`. Coller les mots faisait rater des rapprochements.

**2. Gabriel déduplique désormais entre sources.** Son ancienne clé de
déduplication incluait l'URL, donc la même offre publiée sur LinkedIn et sur un
job board survivait en double et était scorée — et facturée — deux fois. La
stratégie unifiée regroupe sur `(entreprise, titre)` seuls et garde la
description la plus longue.

### 3.4 Coût de migration de la clé de cache

`job_key` change (`ARCHITECTURE.md` § 5.2) : les caches existants
(`seen_jobs.json`, 1 689 entrées ; `seen_jobs_camila.json`, 234) ne
correspondent plus. Ils ne stockent que des hachages, donc les clés ne peuvent
pas être recalculées depuis leur propre contenu.

Conséquence : au premier run sous la nouvelle clé, les offres déjà vues qui sont
**encore présentes dans les flux** seront re-scorées une fois. Le coût n'est pas
proportionnel à la taille du cache mais au volume d'un run — de l'ordre de
70 à 100 offres, soit environ \$0.35 à \$0.50 une seule fois.

Atténuation disponible au bloc 5 : amorcer la table `seen_jobs` à partir des URL
déjà présentes dans les `.xlsx`, plutôt que depuis les caches JSON.

### 3.2 Tarifs Apify

Vérifiés sur les pages publiques des acteurs le 09/09/2026, et encodés dans
`cost_guard.APIFY_PRICING` :

| Acteur | Par démarrage | Par résultat | Comportement face au plafond |
|---|---|---|---|
| `easyapi/infojobs-job-scraper` | \$0.09 | \$0.00299 | **plafond IGNORÉ** — 48/URL quoi qu'on demande (mesuré 19/07/2026 : 108 demandés → 432 reçus) |
| `curious_coder/linkedin-jobs-scraper` | \$0.00 | \$0.001 | **plafond RESPECTÉ** — mesuré 12/09/2026 : 12 URL × `count=100` → exactement 100 chacune, 1200 au total |

Le modèle distingue donc **trois états**, parce que les confondre fausse
l'estimation dans un sens ou dans l'autre :

| État | Encodage | Effet sur l'estimation |
|---|---|---|
| Plafond respecté (mesuré) | `cap_honoured=True` | Le plafond demandé **est** le plafond. Baisser `max_per_search` baisse le coût. Aucun avertissement. |
| Plafond ignoré (mesuré) | `observed_results_per_url=N` | On facture sur N/URL, quoi qu'on demande. Baisser le plafond ne baisse **pas** le coût. Avertissement émis. |
| Jamais mesuré | ni l'un ni l'autre | On suppose le plafond respecté, mais l'estimation est annoncée comme un **plancher**. Avertissement émis. |

Encoder « plafond respecté » comme `observed_results_per_url=100` aurait été
faux : ce 100 serait alors traité comme un plancher indépendant, et
sur-estimerait toute demande plus petite.

Formule de pire cas, généralisée à tout acteur :

```
worst_case = max(max_items_requested, observed_results_per_url × n_search_urls)
```

Réserve restante, inscrite dans le code : Apify facture possiblement l'usage
plateforme en plus du prix par événement. L'estimation reste donc un **plancher
fiable**, pas un plafond garanti — d'où l'intérêt d'un `max_cost_per_run` bas. Un acteur sans
entrée tarifaire lève `UnknownActorError` au lieu d'être estimé à \$0.

Coût des runs actuels, aux paramètres d'aujourd'hui :

| Run | Démarrages | Résultats (pire cas) | Coût | Décision |
|---|---|---|---|---|
| InfoJobs (9 × 12) | 1 | 432 | \$1.38 | `NEEDS_CONFIRMATION` |
| LinkedIn (6 × 2 × 100) | 12 | 1200 | \$1.20 (**vérifié en réel**) | `NEEDS_CONFIRMATION` |
| Les deux | 13 | 1632 | \$2.58 | `NEEDS_CONFIRMATION` |

---

### 3.5 Trois défauts trouvés en câblant les connecteurs (bloc 3)

Chacun existait dans les notebooks et était invisible. Les trois sont couverts
par un test qui affirme le nouveau comportement.

**1. Pagination InfoJobs : la même offre comptée plusieurs fois.** Dans le dump
réel, une même offre (même identifiant `of-i…`) atteinte depuis la page 2 et la
page 3 des résultats produit deux URL qui ne diffèrent que par `page=` et
`sortBy=`. Elles survivaient à la déduplication par URL, étaient scorées deux
fois, et recevaient un `job_key` différent à chaque run — donc le cache des
offres vues ne les reconnaissait jamais. `page`, `sortBy` et
`applicationOrigin` rejoignent `TRACKING_PARAMS`, et les mappers dédupliquent
désormais sur l'URL nettoyée.

Effet mesuré sur le dump : **237 → 199** offres mappées, et les paires
(entreprise, titre) encore en double passent de **35 à 2**. Soit 38 offres
facturées en double à chaque run.

**2. Localisation Xarxanet toujours vide.** Le notebook cherchait un
`<span class="field-label">` dans un `<li>`. La page rend en réalité un
`<h2 class="field-label">Població</h2>` suivi d'un `<div class="field-value">`,
dans un bloc `field-job-offer-location`, hors de tout `<li>`. Le sélecteur ne
matchait donc jamais. C'est ce qui obligeait la pénalité Maresme de Camila à se
rabattre sur le titre. Corrigé : **7/7** offres localisées.

**3. Localisation et entreprise absentes côté RSS.** Les flux exposent ces
champs sous des noms différents (`job_listing_location` / `job_listing_company`
sur Jobicy, `region` sur WeWorkRemotely) ; le notebook n'en lisait aucun. Par
ailleurs son extraction d'entreprise ne gérait que `"Titre at Entreprise"`,
jamais `"Entreprise: Titre"` — la convention de WeWorkRemotely, le plus gros
flux vivant. Résultat mesuré après correction : **243/263** offres avec
localisation et entreprise, contre 0 auparavant.

---

## 4. Moteur de scoring paramétré

Base : la version Camila (retry sur JSON invalide, `max_tokens=2000`).

**Principe central : le LLM ne rend plus de verdict.** Il rend des sous-scores et
des booléens. Moyenne pondérée, pénalités, bonus et seuils vivent en Python, à un
seul endroit.

```python
def build_eval_prompt(profile: UserProfile, job: JobPosting) -> str:
    """Assemble : candidat (CV) + offre + un bloc par dimension (son rubric)
    + un bloc par llm_flag (sa definition) + le schéma JSON de sortie.
    Ne contient NI formule de pondération, NI seuil de verdict."""

def score_job(client, profile: UserProfile, job: JobPosting,
              max_attempts: int = 2) -> ScoredJob | None: ...

def compute_verdict(profile, subscores, flags, job) -> Verdict:
    # 1. moyenne pondérée — poids depuis le profil
    base = sum(subscores[d.key] * d.weight for d in profile.dimensions)

    # 2. flags renvoyés par le LLM
    score, notes = base, []
    for f in profile.llm_flags:
        if flags.get(f.key):
            score += f.adjustment
            notes.append((f.target, f"[{f.adjustment:+.1f}] {f.note}"))

    # 3. pénalités calculées en Python (jamais demandées au LLM)
    for p in profile.keyword_penalties:
        haystack = " ".join(getattr(job, fld, "") or "" for fld in p.fields).lower()
        if any(k in haystack for k in p.keywords):
            score += p.adjustment
            notes.append(("red_flags", f"[{p.adjustment:+.1f}] {p.note}"))

    score = min(10.0, max(0.0, round(score, 1)))

    # 4. seuils — UNE SEULE source de vérité
    t = profile.thresholds
    verdict = "YES" if score > t.yes_above else "MAYBE" if score >= t.maybe_above else "NO"
    return Verdict(score=score, verdict=verdict, notes=notes, base_score=round(base, 1))
```

Ce que ça règle :

| Problème dans l'existant | Résolution |
|---|---|
| Seuils 7.0/5.0 écrits dans le prompt **et** en Python (Camila), qui recalculait le verdict après pénalités | Le prompt ne les mentionne plus. Un seul calcul. |
| Formule de pondération dans le prompt : le LLM faisait l'arithmétique | Faite en Python. Le LLM ne rend que des entiers 0-10. |
| Gabriel n'a aucune pénalité, Camila en a 6, dans du code différent | Même fonction, listes vides côté Gabriel |
| `weighted_score` renvoyé par le LLM, possiblement incohérent avec ses propres sous-scores | Plus demandé. `base_score` est recalculé. |

Bénéfice secondaire : le prompt raccourcit (plus de formule, plus de seuils, plus
de calibration sur le total), donc coûte moins par offre.

**Prompt caching : ne pas tenter en l'état.** Le préfixe minimum cachable de
Haiku 4.5 est de **4096 tokens**. Le préfixe partagé (system + CV + barèmes) fait
~1 900 tokens : le cache ne s'activerait pas, silencieusement, sans erreur. Gain
nul avec l'illusion d'un gain.

### 4.1 Ce que le différentiel a révélé (bloc 4)

Le test rejoue les 938 lignes du `.xlsx` de Gabriel et les 61 de celui de
Camila. Il sépare volontairement deux questions.

**La logique portée est reproduite exactement.** En rejouant les ajustements et
les seuils sur le score de base que le pipeline avait lui-même enregistré :
938/938 et 61/61 verdicts identiques, 43 lignes Camila portant des ajustements.
Une seule divergence de score, de 0.1, sur une base à deux décimales (7.35) —
différence de convention d'arrondi, voir plus bas.

**L'arithmétique, elle, était fausse — et c'est la justification de tout le
bloc.** Le modèle calculait lui-même la moyenne pondérée :

| | Gabriel | Camila |
|---|---|---|
| Lignes vérifiées | 938 | 61 |
| Notre moyenne est exacte | 938/938 | 61/61 |
| Le total du modèle divergeait | **812 (87 %)** | **51 (84 %)** |
| Erreur moyenne | **+0.27** | **+0.27** |
| Erreur max | +1.4 | +1.1 |
| Le modèle sous-notait | 679/812 (84 %) | 50/51 (98 %) |

Le modèle se trompait dans ~85 % des cas, systématiquement **vers le bas**, avec
des écarts allant jusqu'à 1.4 point. Des offres ont donc été classées `NON`
alors que leurs propres sous-scores donnaient `OPPORTUNISTE`. Déplacer ce calcul
en Python n'est pas un raffinement d'architecture : ça corrige un défaut de
notation qui affectait un run sur deux.

**Trois lignes où le verdict contredit son propre score.** Côté Gabriel, 3
lignes sur 938 portent un verdict que leurs propres seuils ne produisent pas
(score 7.4 → `OPPORTUNISTE` alors que le seuil est `> 7.0`). Le verdict était
demandé au modèle, qui pouvait donc être en désaccord avec lui-même. Il est
désormais dérivé du score, ce qui rend ce cas impossible par construction.

### 4.2 Deux choix de calcul explicites

**Arrondi au plus proche, à la moitié supérieure.** `round()` de Python est un
arrondi bancaire sur des flottants binaires : `round(7.85, 1)` donne `7.8`. Le
moteur utilise `Decimal` avec `ROUND_HALF_UP`, qui donne `7.9`. C'est la source
de l'unique divergence de 0.1 relevée ci-dessus.

**Somme des ajustements, puis un seul clamp dans 0–10.** Le notebook clampait
après *chaque* pénalité, ce qui rend le résultat dépendant de l'ordre de
déclaration : une offre descendue sous 0 par les pénalités puis relevée par le
bonus n'atterrit pas au même endroit selon la position du bonus dans la liste.
Réordonner les flags dans un profil JSON aurait silencieusement changé les
scores. Vérifié sur les 61 lignes réelles : sommer d'abord reproduit 61/61 et
est indistinguable du clamp en ordre de déclaration ; clamper les pénalités
d'abord n'en reproduit que 60.

---

## 5. État et persistance

### 5.1 Suppression de l'état global

Dans l'existant, `client`, `seen`, `CACHE_FILE`, `GABRIEL_PROFILE`,
`EXCLUDED_KEYWORDS` sont des globales lues au fond des fonctions. Deux
utilisateurs simultanés se marcheraient dessus. Tout devient argument :

```python
def run(profile: UserProfile,
        store: Store,
        secrets: Secrets,
        opts: RunOptions) -> RunReport: ...

class RunOptions(BaseModel):
    fetch_full_descriptions: bool = False           # ex-FETCH_FULL
    use_seen_cache: bool = True                     # ex-USE_CACHE
    export_verdicts: list[str] = ["YES", "MAYBE"]   # ex-SHOW_VERDICTS
    generate_text: bool = True                      # ex-GENERATE_EMAILS
    scoring_workers: int = 10
    writing_workers: int = 5
    confirmed_fingerprint: str | None = None        # 2e confirmation de coût

class RunReport(BaseModel):
    run_id: str
    status: Literal["OK", "NEEDS_CONFIRMATION", "REJECTED_OVER_HARD_CAP"]
    cost: RunEstimate
    counts: dict[str, int]   # fetched, deduped, excluded, already_seen, scored, par verdict
    results: list[ScoredJob]
```

`Secrets` = `ANTHROPIC_API_KEY` + `APIFY_TOKEN`, lus côté serveur depuis
l'environnement. **Pas par utilisateur** : ce sont les clés du propriétaire, les
utilisateurs consomment son quota. C'est ce qui rend le plafond de coût par
utilisateur nécessaire, et pas seulement prudent.

### 5.2 Clé de déduplication unifiée

Les deux pipelines divergeaient. On retient la stratégie Camila, plus robuste,
plus le `_strip_tracking` de Gabriel (26 paramètres de tracking) :

```python
def job_key(job) -> str:
    return md5(norm(title) + norm(company) + strip_tracking(url))[:12]
```

Pour la déduplication intra-run : grouper par `(company, title)` et **garder le
résumé le plus long** (stratégie Camila) plutôt que « le premier gagne », parce
qu'elle conserve plus d'information à scorer.

### 5.3 Tables Supabase

`user_id` est l'identifiant Supabase Auth partout ; c'est lui qui porte la RLS
(chaque utilisateur ne voit que ses lignes).

| Table | Colonnes | Rôle |
|---|---|---|
| **`profiles`** | `id`, `user_id`, `display_name`, `config jsonb`, `cv_text`, `updated_at` | Remplace `profiles/*.json`. `config` = `UserProfile.model_dump()` moins le CV. |
| **`runs`** | `id`, `user_id`, `profile_id`, `started_at`, `finished_at`, `status`, `cost_decision`, `cost_estimate jsonb`, `cost_fingerprint`, `counts jsonb` | Un run = une ligne. Le `fingerprint` y est stocké : c'est ce qui permet à la 2e confirmation d'arriver dans une requête séparée, plus tard, sans état en mémoire. |
| **`seen_jobs`** | `user_id`, `job_key`, `first_seen_at` — PK `(user_id, job_key)` | Remplace `seen_jobs.json`. Le cache devient naturellement par utilisateur, ce qu'un fichier global ne permettait pas. |
| **`job_results`** | `id`, `user_id`, `run_id`, `job_key`, `title`, `company`, `url`, `source`, `published`, `location`, `verdict`, `score`, `base_score`, `breakdown jsonb`, `one_liner`, `match_signals text[]`, `gaps text[]`, `red_flags text[]`, `flags jsonb`, `extra jsonb`, `generated_text`, `salary_range_market`, `evaluated_at` — unique `(user_id, url)` | **La destination principale des résultats** (voir section 8). L'index unique remplace `load_tracker_urls()`, qui relisait un `.xlsx` pour éviter de re-scorer. |
| **`applications`** | `id`, `user_id`, `job_result_id`, `status`, `priority`, `target_salary`, `notes`, `updated_at` | Remplace la feuille « Tracker ». Seule table que l'utilisateur *modifie*. |
| **`raw_dumps`** | `id`, `user_id`, `run_id`, `connector`, `actor_id`, `apify_run_id`, `items_count`, `storage_path`, `saved_at` | Généralise `infojobs_raw_dump.json`, pour rejouer un mapping à coût nul. |

Deux remarques :

- Le dump brut fait 1,5 MB. En `jsonb` c'est jouable mais désagréable :
  **Supabase Storage** pour le payload, `storage_path` en base.
- **La séparation `job_results` / `applications` corrige un bug de l'existant.**
  Aujourd'hui la feuille Tracker est recréée à chaque run
  (`if "Tracker" in wb.sheetnames: del wb["Tracker"]`) : tout statut « À
  postuler » modifié à la main est écrasé au run suivant. Le comportement
  souhaité, validé, est que le suivi de candidature survive aux runs.

### 5.4 Notes d'implémentation (bloc 5)

**La cible de non-régression, ce sont les notebooks, pas les `.xlsx`.** Les
fichiers `.xlsx` sur disque ont été écrits par des versions antérieures et ne
contiennent pas les mêmes colonnes que le code actuel (celui de Gabriel a 15
colonnes + une vide, là où `COLUMNS` en déclare 16). Le test compare donc les
en-têtes et largeurs produits aux listes `COLUMNS` / `TRACKER_COLS_DEF` lues par
`ast` dans les notebooks, puis réimporte les 999 lignes réelles pour vérifier le
contenu.

**Deux champs manquaient au bloc 1, ajoutés ici.** `priority_labels` (🔴/🟠/🟡
chez Gabriel, Alta/Media/Baja chez Camila) et `default_application_status`
(« À postuler » / « Pendiente ») n'avaient pas été capturés. Ils sont désormais
émis par l'adaptateur et couverts par `test_profile_completeness.py`.

**L'export réécrit toujours le fichier.** Le notebook ajoutait les nouvelles
lignes à un classeur existant, ce qui a fini par accumuler trois formats de
colonnes incompatibles dans un même fichier. La source de vérité étant
`job_results`, un classeur périmé n'a rien à préserver.

**Les couleurs de verdict sont indexées sur la valeur interne** (`YES`/`MAYBE`/
`NO`), jamais sur le libellé affiché. Dans le notebook de Camila il fallait
maintenir les deux en phase à la main, avec un commentaire de quatre lignes pour
prévenir du piège.

**Les compteurs du résumé viennent des résultats en mémoire**, pas d'une
relecture de la colonne d'affichage — ce que faisait le notebook, et qui cassait
dès que les libellés étaient traduits.

**Isolation par utilisateur dès maintenant.** `JsonStore` écrit un répertoire
par utilisateur et refuse un `user_id` qui ressemble à un chemin, de sorte que
la forme du stockage local reflète ce que la RLS Supabase imposera.

---

## 6. Export et génération de texte

### 6.1 Un seul mécanisme de rédaction

La lettre longue de Gabriel et l'email court de Camila deviennent deux configs du
même moteur, à la place de deux fonctions séparées :

```python
class WritingConfig(BaseModel):
    kind: Literal["cover_letter", "email"]
    enabled: bool = True
    model: str = "claude-sonnet-4-6"
    language: str
    min_words: int | None = None
    max_words: int | None = None
    salutation: str | None = None          # "Hola equipo de {company},"
    closing_sentence: str | None = None    # phrase finale imposée mot pour mot
    required_paragraphs: list[str] = []
    verbatim_phrases: list[str] = []
    banned: list[str] = []
    include_company_context: bool = False  # la recherche DuckDuckGo
    generate_for_verdicts: list[Verdict] = ["YES"]
    extra_rules: list[str] = []
```

Correspondance des 10 règles de `COVER_LETTER_SYSTEM` : 1→`extra_rules`,
2→`banned` (tirets cadratins), 3→`banned` (liens GitHub), 4→`min_words: 550`,
5→`required_paragraphs` (le paragraphe « One small aside: »),
6→`verbatim_phrases` (la phrase Amazon), 7→`extra_rules` (les 4 certifications),
8→`closing_sentence`, 9→`salutation`, 10→`extra_rules` (la structure). Les 9
règles de `EMAIL_SYSTEM` de Camila rentrent de la même façon, avec
`max_words: 150` et `include_company_context: false`.

Une seule fonction `generate_text(client, profile, result)` construit le system
prompt depuis cette config.

### 6.2 Colonnes d'export paramétrables

```python
class ExportColumn(BaseModel):
    label: str          # "Titre" / "Título"
    width: int
    value: str          # "field:title" | "computed:score_breakdown"
                        # | "flag:catalan_imprescindible"
```

`computed:` renvoie vers un petit registre nommé dans `export/computed.py` —
`run_date`, `verdict_display`, `score_breakdown`, `priority`, `target_salary`,
`source_label`, `published_date`, `application_status`. Pas d'`eval`, pas de
mini-langage d'expressions : un dict `nom -> fonction`. C'est ce qui permet aux
16 colonnes de Gabriel et aux 14 de Camila (dont `Catalán requerido`, via
`flag:`) de sortir du même code.

Le squelette (en-têtes stylés, freeze panes, autofilter, bordures,
append-si-URL-nouvelle, feuille résumé) était déjà identique dans les deux
notebooks : il est repris tel quel, seul `COLUMNS` devient une donnée.

---

## 7. Découpage d'implémentation

Six blocs, chacun testable seul, chacun livrable sans casser le précédent. Les
notebooks actuels continuent de tourner jusqu'au bloc 6.

| # | Bloc | Contenu | Test qui le valide | Statut |
|---|---|---|---|---|
| **1** | **Profil + adaptateur** | `profile.py`, `tools/build_profiles.py`, `profiles/*.json`, CV hors git | 89 vérifications comparant les profils aux valeurs lues **en direct** dans les notebooks | **FAIT** |
| **2** | **Contrat + filtres** | `jobs.py`, `filters.py` (normalisation, `strip_tracking`, dédup, les 3 `kind` d'exclusion) | 48 vérifications, dont un test **différentiel** : les fonctions d'origine sont exécutées depuis les notebooks et doivent rendre le même verdict que les nouvelles sur le même corpus. Zéro réseau. | **FAIT** |
| **3** | **Connecteurs** | `connectors/`, `plan()` + `fetch()` pour les 4, `collect.py` câble le garde-fou | 82 vérifications, dont une **preuve d'ordonnancement** (un connecteur espion atteste que `fetch()` n'est jamais atteint sans feu vert). RSS et Xarxanet en réseau réel, InfoJobs rejoué depuis le dump. **`fetch()` LinkedIn jamais exécuté.** | **FAIT** |
| **4** | **Scoring** | `scoring.py`, `prompts.py`, verdict en Python uniquement | 74 vérifications, dont un différentiel sur les **999 lignes réelles** des deux `.xlsx`. Aucun appel Haiku. | **FAIT** |
| **5** | **Persistance + résultats** | `store/`, mapping vers `job_results`, `export/excel.py` (secondaire) | 93 vérifications. L'export est comparé colonne par colonne aux `COLUMNS` lues **dans les notebooks**, sur les 999 lignes réelles réimportées. `supabase_store.py` lève `NotImplementedError`. | **FAIT** |
| **6** | **Pipeline + bascule** | `pipeline.run()`, `writing.py`, notebooks réduits à 9 cellules | 69 vérifications sur le pipeline complet avec un client factice : garde-fou, scoring, retry, génération, persistance, export, isolation entre profils. Aucun appel réel. | **FAIT** (run réel en attente de feu vert) |

### 7.1 Conséquence de la bascule : `legacy/`

Quatre suites comparaient le nouveau code aux **notebooks d'origine**, en les
lisant plutôt qu'en recopiant leurs valeurs — c'est ce qui rendait ces tests
crédibles. Transformer les notebooks en lanceurs détruisait cette référence.

Les originaux sont donc figés sous `legacy/` (gitignoré : ils contiennent
encore le nom du candidat dans `COVER_LETTER_SYSTEM`), et `legacy_values.py`
pointe dessus. `legacy/README.md`, lui, est versionné et explique pourquoi.

`legacy_values.py` lève désormais une erreur explicite si les fichiers manquent,
plutôt que de laisser les tests perdre silencieusement leur point de
comparaison.

### 7.2 Les notebooks comme lanceurs

Chacun passe de 20 à 9 cellules, sans aucune logique métier :

| Cellule | Rôle |
|---|---|
| 2 | Charge le profil, le store et les secrets |
| 4 | **Estimation du coût** — aucun appel réseau, affiche l'empreinte à confirmer |
| 6 | `run()` — ne dépense que si `CONFIRM` contient l'empreinte |
| 8 | Relit `job_results` depuis le store, sans rien relancer |

Le garde-fou est donc dans le chemin par défaut : ouvrir le notebook et tout
exécuter ne peut pas lancer un run payant tant que l'empreinte n'a pas été
recopiée à la main. `RunOptions.max_jobs_to_score` ajoute un plafond sur le
nombre d'offres scorées, pour qu'une faute de frappe dans une liste de mots-clés
ne se traduise pas en facture Haiku.

---

## 8. Destination des résultats : `job_results`, pas Excel

**Le produit final n'est pas un fichier Excel.** L'Excel était le format de
sortie d'un usage solo. Pour l'application multi-utilisateurs, la destination des
résultats de scoring est la table **`job_results`** en base Supabase, que le
frontend Lovable interroge directement pour afficher les offres dans l'UI web
(liste, scores, verdicts, breakdown).

Conséquences sur le découpage ci-dessus :

- Le **bloc 5** garde toute son utilité : `store/json_store.py` sert en
  développement et en test avant que Supabase soit branché, et la logique de
  mapping des résultats est la même dans les deux cas.
- **`export/excel.py` devient une fonctionnalité secondaire** : un export
  optionnel que l'utilisateur pourra déclencher depuis l'app s'il veut une copie
  Excel de ses résultats. Ce n'est pas le mécanisme de restitution.
- Le test de comparaison colonne par colonne avec le `.xlsx` actuel reste utile,
  mais comme **test de non-régression de migration** — pas comme validation que
  l'Excel est le livrable.

Quand on arrivera aux blocs 5 et 6, la priorité est donc de remplir proprement
`job_results` ; l'export Excel est un bonus.

---

## Annexe A — Corrections apportées à l'existant

Corrections réalisées avant l'architecture, conservées ici pour mémoire.

### A.1 Bug d'échappement d'accolades (corrigé)

`job_scout_camila.ipynb` cellule 10 : le template JSON du prompt était écrit
`{{{{{{{{` dans une f-string, soit `{{{{` après rendu. Le modèle recevait donc un
exemple de JSON **invalide** à imiter — vraisemblablement la cause du
`EVAL_MAX_ATTEMPTS = 2` ajouté pour les « JSON invalides/tronqués ». Corrigé en
`{{` (rendu : `{`). Vérifié : l'exemple rendu est désormais parseable.

*Reste en attente* : la mesure empirique du taux d'erreur avant/après. Elle n'est
pas gratuite (le dump supprime le coût Apify, pas les appels Haiku) — environ
\$0.38 sur un échantillon de 40 offres, \$2.20 sur les 229.

### A.2 Cookie LinkedIn supprimé

`LI_AT` / `JSESSIONID` étaient injectés dans le header `Cookie` du dict
`request_headers` **générique** de `parse_feeds`, donc envoyés à toutes les URLs
de `RSS_FEEDS`. Or aucune de ces 6 URLs ne pointait vers `linkedin.com` : le
cookie n'atteignait jamais LinkedIn et ne débloquait rien. En revanche il
divulguait un identifiant de session LinkedIn à 5 hôtes tiers
(weworkremotely.com, hnrss.org, arbeitnow.com, remotive.com, jobicy.com).

Résidu de l'« Option A » du README, remplacée par Apify (« Option C ») sans
nettoyage. Supprimé du notebook, du `.env` et du README, avec une note explicite
« ne pas réintroduire ».

LinkedIn continue d'être scrapé, via `scrape_linkedin_apify` → l'acteur Apify,
qui s'authentifie côté serveur Apify et ne recevait pas ce cookie.

### A.3 État réel des flux RSS (mesuré le 09/09/2026, sans header Cookie)

| Flux | HTTP | Entrées | Verdict |
|---|---|---|---|
| weworkremotely (programming) | 200 | 25 | vivant |
| weworkremotely (devops) | 200 | 18 | vivant |
| hnrss.org | 200 | 20 | vivant |
| jobicy.com | 200 | 200 | vivant |
| arbeitnow.com `/feed` | 301 → page d'accueil HTML | 0 | **mort** |
| remotive.com | 404 | 0 | **mort** |

Les deux flux morts sont retirés de `profiles/gabriel.json`. À noter : la
suppression du cookie n'a réparé aucun flux — l'hypothèse selon laquelle il
causait l'échec de Jobicy a été testée (cookie factice, avec contrôle) et
**réfutée**. Jobicy fonctionne aujourd'hui pour une raison qui lui est propre.

### A.4 Comptages corrigés

L'audit initial donnait quelques comptages approximatifs. Valeurs exactes, lues
par `tests/legacy_values.py` :

| | Audit initial | Valeur réelle |
|---|---|---|
| `EXCLUDED_KEYWORDS` Gabriel | 13 | **16** |
| `EXCLUDED_KEYWORDS` Camila | 4 | **5** |
| `XARXANET_FILTER_KEYWORDS` | 14 | **13** |
| `MARESME_R1_TOWNS` | 28 | **25** |

### A.5 Fuites de données personnelles

| Élément | Statut |
|---|---|
| `camila_cv_context.txt` (nom complet, université, moyenne) | retiré du suivi git, déplacé en `profiles/cv/camila.txt` |
| `GABRIEL_PROFILE` en dur dans `job_scout.ipynb` cellule 8 | extrait en `profiles/cv/gabriel.txt`, la cellule lit le fichier |
| `COVER_LETTER_SYSTEM` (cellule 13) nomme le candidat et sa phrase Amazon | migre dans `profiles/gabriel.json` au **bloc 6** |
| Sorties d'exécution des notebooks (noms d'entreprises réelles, scores, nom de Camila) | **non traité** |

⚠️ **L'historique git conserve tout.** `git rm --cached` arrête le suivi futur
mais le commit `8e73fec` contient toujours le CV de Camila et le profil de
Gabriel. Purger l'historique demande une réécriture (`git filter-repo` ou BFG),
qui réécrit tous les hash de commits. À décider séparément.

---

## 9. Supabase — schéma, RLS et `SupabaseStore`

Le SQL complet est dans `supabase/schema.sql`, idempotent, à appliquer une fois
dans l'éditeur SQL du projet. `jobscout/store/supabase_store.py` implémente le
même `Protocol` que `JsonStore` ; les deux coexistent.

### 9.1 Modèle de sécurité — le point à comprendre

| Qui | Clé utilisée | RLS |
|---|---|---|
| Le backend (ce moteur) | `SUPABASE_SERVICE_ROLE_KEY` | **contournée** |
| Le frontend Lovable | clé `anon` + JWT de l'utilisateur | **appliquée par Postgres** |

Le backend exécute des runs *pour le compte d'un* utilisateur, hors de toute
session navigateur : il lui faut donc la clé service-role, qui ignore la RLS.
**L'isolation entre utilisateurs dans ce processus repose donc sur le filtre
`user_id` présent dans chaque requête Python, pas sur la base.**

C'est un arbitrage assumé, avec une arête vive : un filtre oublié ferait fuiter
les résultats d'un utilisateur vers un autre. Trois garde-fous en conséquence :

1. Chaque méthode filtre explicitement sur `user_id`.
2. `_guard()` refuse un `user_id` vide — avec la clé service-role, un filtre
   vide ne renverrait pas « aucun résultat » mais « les résultats de tout le
   monde ».
3. Les écritures vérifient l'appartenance de **tout le lot** avant d'écrire
   quoi que ce soit, dans cet ordre précis (le bug corrigé au bloc 5).

La RLS n'est pas décorative pour autant : c'est elle qui protège le frontend,
qui lui se connecte avec la clé `anon` et le JWT de l'utilisateur.

`SupabaseStore(url, anon_key, access_token=jwt)` fonctionne aussi, et dans ce
cas Postgres applique la RLS en plus des filtres Python.

### 9.2 Ce que la RLS garantit

Une policy par table, couvrant les quatre commandes, avec `using` (quelles
lignes sont visibles) **et** `with check` (quelles lignes peuvent être écrites).
Séparer les deux est ce qui empêche un utilisateur d'insérer une ligne
appartenant à quelqu'un d'autre. `force row level security` l'applique aussi au
propriétaire de la table, pour qu'une erreur ne soit pas masquée par un accès
privilégié pendant le développement. Rien n'est accordé à `anon` : un visiteur
non authentifié ne voit aucune ligne.

### 9.3 Variables d'environnement

```
SUPABASE_URL                https://<ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY   côté serveur uniquement, jamais dans le frontend
SUPABASE_ANON_KEY           pour le frontend, ou avec un access_token utilisateur
```

Aucune n'est en dur dans le code, comme pour `ANTHROPIC_API_KEY` et
`APIFY_TOKEN`.

### 9.4 Reste à valider sur un projet réel

Le test couvre 103 points hors-ligne, mais six choses ne peuvent pas être
prouvées sans projet :

1. Que le SQL s'applique effectivement (il est vérifié comme texte, jamais exécuté).
2. **Que la RLS isole réellement deux utilisateurs** — la vérification la plus
   importante : deux comptes, chacun interrogeant avec son propre JWT.
3. Que `upsert(..., ignore_duplicates=True)` ne renvoie que les lignes réellement
   insérées, ce sur quoi `save_results()` compte pour son retour.
4. Que la syntaxe de jointure `job_results!inner(url)` renvoie la forme imbriquée
   attendue par `get_applications()`.
5. La gestion des horodatages : `timestamptz` en entrée, chaîne ISO en sortie.
6. Le comportement réseau réel : délais, réessais, limites de débit.
