import os

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Modele texte pour l'analyse HTML, la generation de scenarios et de scripts
<<<<<<< HEAD
TEXT_MODEL = os.getenv("TEXT_MODEL", "llama3.2:1b")

# Modele multimodal (vision) pour l'analyse de captures d'ecran
# Modeles Ollama compatibles : llava, llava-llama3, bakllava, llama3.2-vision, moondream
VISION_MODEL = os.getenv("VISION_MODEL", "llava:7b")
VISION_TIMEOUT = int(os.getenv("VISION_TIMEOUT", "180"))

# Taille max de l'image uploadee (en octets) avant d'etre envoyee au modele
MAX_IMAGE_SIZE = int(os.getenv("MAX_IMAGE_SIZE", str(8 * 1024 * 1024)))  # 8 Mo

# ── OCR (Tesseract) — étape 1 du pipeline vision, remplace la transcription
# par LLM pour éliminer les hallucinations de type "champ email/password
# inventé". Nécessite le binaire Tesseract-OCR installé séparément :
# https://github.com/UB-Mannheim/tesseract/wiki
# Laisser vide ("") si tesseract est déjà dans le PATH Windows.
TESSERACT_CMD = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

# Confiance OCR minimale (0-100) pour garder un mot détecté. En dessous,
# c'est probablement du bruit visuel (icône, texture...) et non du vrai texte.
# Baissé de 40 -> 28 : les onglets/menus secondaires (ex: "Correcteur",
# "Dictionnaire" dans Google Traduction) sont souvent en texte plus petit
# ou moins contrasté que le contenu principal -> confiance OCR plus faible.
# Un seuil trop strict les faisait disparaitre AVANT même d'atteindre le LLM.
OCR_MIN_CONFIDENCE = int(os.getenv("OCR_MIN_CONFIDENCE", "28"))
=======
TEXT_MODEL = os.getenv("TEXT_MODEL", "llama3.2:3b")
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

REPORTS_DIR = "reports"
UPLOAD_DIR  = "uploads"

<<<<<<< HEAD
# Dossier où sont enregistrées les captures d'écran prises à la fin de
# CHAQUE scénario (succès ou échec) par le script Selenium généré. Utilisé
# par script_service (écriture) et report_service (lecture pour embarquer
# l'image en base64 dans le rapport HTML autonome).
SCREENSHOTS_DIR = os.getenv("SCREENSHOTS_DIR", "screenshots")

# Timeouts (en secondes)
TEXT_TIMEOUT      = int(os.getenv("TEXT_TIMEOUT",      "360"))
# Abaissé de 120 -> 45 : un scénario Selenium normal (3 actions max, cf.
# script_service._generate_actions) ne devrait jamais approcher 120s.
# Ce timeout ne sert que de garde-fou contre un VRAI blocage (page qui ne
# répond jamais). Avec AGENT_MAX_RETRIES_PER_SCENARIO tentatives, un
# timeout trop haut multiplie le pire cas (ex: 30 scenarios x 2 tentatives
# x 120s = 2h de pire cas cumulé). 45s laisse largement le temps aux
# WebDriverWait internes (voir ELEMENT_WAIT_TIMEOUT) de se déclencher
# proprement avant la coupure subprocess.
EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", "45"))

# Timeout (en secondes) de CHAQUE WebDriverWait / pick_visible() DANS le
# script Selenium généré (script_service.py). Abaissé de 15 -> 8 : avec
# jusqu'à 3 éléments par scénario, un élément introuvable à 15s coûtait
# jusqu'à 45s avant même le fallback texte -> gros contributeur du temps
# total avec beaucoup de scénarios.
ELEMENT_WAIT_TIMEOUT = int(os.getenv("ELEMENT_WAIT_TIMEOUT", "8"))

# Nombre max de scenarios a generer par interface.
# Relevé de 6 -> 20 : avec la garantie de couverture (scenario_service.
# _ensure_element_coverage), une page riche en boutons/liens distincts
# (ex: 3 providers de connexion + plusieurs onglets) peut légitimement
# produire plus de 14 features. Mieux vaut plafonner haut et laisser
# l'utilisateur réduire via l'env MAX_SCENARIOS si besoin, que de couper
# silencieusement des fonctionnalités réellement détectées.
MAX_SCENARIOS = int(os.getenv("MAX_SCENARIOS", "30"))

# ── FIX COUVERTURE (bug racine identifié) ──────────────────────────────────
# html_parser_service._clean_html() coupait le HTML nettoyé (script/style
# déjà retirés) à 40 000 caractères AVANT même que BeautifulSoup n'extraie
# le moindre élément. Sur une grosse page (portail ministériel avec
# mega-menu + actualités + rapports + téléchargements + galeries + footer
# social...), le HTML nettoyé dépasse très largement 40k caractères : tout
# ce qui vient après le header/mega-menu (donc presque tout le contenu
# métier) était silencieusement invisible pour le reste du pipeline, qui
# ne pouvait alors générer des scénarios QUE sur les quelques centaines de
# premiers caractères (typiquement : les sous-menus de navigation). Ce
# n'était PAS un problème de "compréhension" du LLM : les features/
# scénarios manquants n'existaient tout simplement plus en amont.
# Valeur relevée à 300 000 (au lieu de 40 000) ; reste configurable au cas
# où une page dépasserait encore cette taille.
#
# FIX 2 (root cause toujours présent au-delà de 300k) : cette limite était
# appliquée AVANT _parse_html_elements(), qui ne fait AUCUN appel LLM — le
# parsing BeautifulSoup n'a donc AUCUNE raison d'être borné par une taille
# de contexte de modèle. Elle ne protégeait en réalité contre rien d'utile,
# elle recréait juste le même bug "bug racine" décrit ci-dessus à un seuil
# plus haut (une page ministérielle avec mega-menu peut dépasser 300k rien
# que pour le header). Les VRAIS points bornés par le contexte du LLM sont
# _detect_page_type (html_code[:2000]) et _llm_fallback_parse (code[:6000])
# — indépendants de cette constante. On relève donc MAX_HTML_CHARS à 2M
# (garde-fou anti-pathologique uniquement, ex: page générée avec des
# milliers de nœuds dupliqués), ce qui couvre en pratique la quasi-totalité
# des pages réelles, aussi riches soient-elles.
MAX_HTML_CHARS = int(os.getenv("MAX_HTML_CHARS", "2000000"))

# ── Détection déterministe de contenu riche (téléchargements / médias) ────
# Extensions de fichiers traitées comme des téléchargements (PDF, rapports,
# documents...) plutôt que comme de simples liens de navigation — voir
# html_parser_service._is_download_link() et navigation_discovery_service
# (catégorie "Téléchargements de documents", toujours générée, jamais
# fusionnée avec la navigation générique).
DOWNLOAD_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".csv", ".odt", ".ods",
}

# Mots-clés (dans le nom de zone calculé par _get_context) signalant une
# zone de CONTENU (actualités, rapports, publications, presse...) plutôt
# qu'une zone STRUCTURELLE (menu, footer boilerplate). Utilisé par
# navigation_discovery_service pour ne PAS fusionner ces zones dans le
# filet de sécurité générique "internal_links:<zone>" — voir le problème
# "actualités/rapports/galeries disparaissent" : elles doivent soit
# obtenir leur propre catégorie déterministe (téléchargements, médias),
# soit repartir vers le Feature Discovery Agent (LLM) pour être comprises
# comme de vraies fonctionnalités ("lire une actualité", "consulter un
# rapport"), jamais être noyées dans un self.get générique "clic + vérifie
# changement de page".
CONTENT_ZONE_KEYWORDS = {
    "actualit", "news", "rapport", "report", "publication", "presse",
    "press", "telecharg", "download", "document", "galerie", "gallery",
    "photo", "video", "media", "média", "article", "blog",
}

# html_parser_service._parse_html_elements() plafonnait aussi le nombre
# total d'éléments UI extraits à 200 (deduped[:200]) — insuffisant pour une
# page avec mega-menu + actualités + rapports + galerie + footer riche.
# Le vrai plafond de scénarios reste MAX_SCENARIOS (appliqué après coverage
# côté scenario_service), donc ce cap-ci n'a plus besoin d'être restrictif.
MAX_UI_ELEMENTS = int(os.getenv("MAX_UI_ELEMENTS", "600"))

# ── Batching LLM (anti-troncature) ────────────────────────────────────────
# Envoyer TOUS les textes/éléments d'un coup à un petit modèle local
# (llava:7b, llama3.2:3b) dépasse souvent son num_predict -> le JSON de
# réponse est coupé -> des éléments/fonctionnalités entiers disparaissent
# silencieusement. On découpe donc le travail en lots plus petits et on
# fusionne les résultats. Voir vision_parser_service.py et scenario_service.py.

# Nombre max de textes OCR envoyés en une seule requête au modèle vision
# pour l'étape de classification (vision_parser_service._classify_elements)
VISION_CLASSIFY_BATCH_SIZE = int(os.getenv("VISION_CLASSIFY_BATCH_SIZE", "18"))

# Nombre max d'éléments UI envoyés en une seule requête au modèle texte
# pour l'étape d'extraction de fonctionnalités (scenario_service._extract_features)
FEATURE_EXTRACT_BATCH_SIZE = int(os.getenv("FEATURE_EXTRACT_BATCH_SIZE", "20"))

# Nombre max de fonctionnalités envoyées en une seule requête pour la
# génération de scénarios (scenario_service._generate_scenarios_from_features)
SCENARIO_GEN_BATCH_SIZE = int(os.getenv("SCENARIO_GEN_BATCH_SIZE", "6"))

# ── Prétraitement OCR (résolution/contraste) ──────────────────────────────
# Si la largeur de l'image est inférieure à ce seuil, elle est agrandie
# avant OCR : Tesseract est nettement plus fiable sur du texte UI (souvent
# 11-14px) quand l'image est suffisamment grande.
OCR_UPSCALE_MIN_WIDTH = int(os.getenv("OCR_UPSCALE_MIN_WIDTH", "1600"))

# Modes de segmentation Tesseract (PSM) essayés et FUSIONNÉS. Le mode 3
# (défaut) suppose une mise en page de document ; il rate souvent du texte
# UI éparpillé (boutons/icônes/nav) car ce n'est pas un paragraphe.
# - psm 11 : "sparse text" (texte épars, sans ordre) — idéal captures d'écran
# - psm 6  : bloc de texte uniforme — complète les zones denses (tableaux, listes)
OCR_PSM_MODES = [m.strip() for m in os.getenv("OCR_PSM_MODES", "11,6").split(",") if m.strip()]

# ── Agent (orchestrateur agentique, tool-calling) ─────────────────────────
# Modèle DÉDIÉ à l'orchestration : llama3.2:3b décroche vite dans une boucle
# de tool-calling multi-tours (mauvais tool choisi, boucle infinie). Un
# modèle 8B avec function-calling natif est le minimum viable pour un agent
# fiable. Les tâches bornées (extraction de features, écriture de scénarios)
# continuent d'utiliser TEXT_MODEL — seule l'orchestration change de modèle.
AGENT_MODEL = os.getenv("AGENT_MODEL", "llama3.1:8b")

# Garde-fou anti-boucle-infinie : nombre max de tours (1 tour = 1 réponse
# du LLM, pouvant contenir plusieurs tool_calls) avant arrêt forcé.
AGENT_MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "40"))

# Nombre max de retries (stratégie alternative) par scénario avant que
# l'agent soit obligé d'abandonner (give_up_on_scenario) plutôt que de
# boucler indéfiniment sur le même échec.
# Abaissé de 2 -> 1 : chaque retry relance TOUT (génération + exécution
# Selenium), donc double quasi le temps du scénario en cas d'échec. Avec
# 30 scénarios ce facteur ×2 pèse plus que le fallback texte lui-même,
# qui reste tenté une fois (le plus utile) avant d'abandonner proprement.
AGENT_MAX_RETRIES_PER_SCENARIO = int(os.getenv("AGENT_MAX_RETRIES_PER_SCENARIO", "1"))

# ── Parallélisation (fix perf pipeline 40+ min) ───────────────────────────
# Nombre de générations de scénarios (appels LLM) lancées en concurrence.
# Attention : si Ollama tourne avec OLLAMA_NUM_PARALLEL=1 (défaut), les
# requêtes seront quand même traitées une par une côté serveur -> il FAUT
# démarrer Ollama avec la variable d'env OLLAMA_NUM_PARALLEL=3 (ou plus)
# pour que cette parallélisation apporte un vrai gain de temps.
SCENARIO_GEN_CONCURRENCY = int(os.getenv("SCENARIO_GEN_CONCURRENCY", "3"))

# Nombre d'instances Chrome headless exécutées en parallèle pendant
# l'étape d'exécution Selenium. Chaque instance consomme ~200-400 Mo de
# RAM ; relevé de 3 -> 6 (~1.5-2.5 Go de RAM en pointe), pour diviser par
# ~2 le temps total d'exécution avec 30 scénarios sans changer le nombre
# de tests exécutés. Rebaisse à 3-4 si ta machine a moins de 8 Go de RAM
# libre ou si tu vois des erreurs "cannot connect to Chrome".
EXECUTION_CONCURRENCY = int(os.getenv("EXECUTION_CONCURRENCY", "6"))
=======
# Timeouts (en secondes)
TEXT_TIMEOUT      = int(os.getenv("TEXT_TIMEOUT",      "360"))
EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", "120"))

# Nombre max de scenarios a generer par interface
MAX_SCENARIOS = int(os.getenv("MAX_SCENARIOS", "6"))
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
