"""
auth_semantics.py
------------------
Détection partagée des éléments UI liés à l'authentification qui NE sont
PAS le formulaire de connexion natif lui-même : lien d'inscription, lien
"mot de passe oublié", boutons de connexion via un fournisseur tiers
(Google, Microsoft, Apple, Facebook, LinkedIn, GitHub...).

POURQUOI UN MODULE SÉPARÉ (plutôt que dupliquer des mots-clés dans
navigation_discovery_service.py ET scenario_service.py) :
Les deux services doivent s'accorder EXACTEMENT sur ce qui est "un lien
d'authentification annexe" :
  - navigation_discovery_service.py doit l'EXCLURE de ses catégories de
    navigation générique (sinon il finit dans un scénario "Liens internes"
    générique, cliquer + vérifier changement de page).
  - scenario_service.py doit le SÉPARER d'une feature de login mixte
    (sinon "Créer un compte" est testé comme un remplissage des champs
    email/mot de passe du login, et plusieurs boutons SSO fusionnés en
    une feature générique perdent leurs fournisseurs respectifs).
Une divergence entre les deux (ex : un mot-clé ajouté d'un seul côté)
réintroduirait silencieusement le bug qu'on corrige ici.

Volontairement 100% déterministe (mots-clés + domaine de destination),
sans appel LLM : ce sont des libellés/domaines stables et bien connus,
un classifieur par règles est plus fiable et moins cher qu'un LLM pour ce
cas précis (même raisonnement que test_planning_service._RULES).
"""
from __future__ import annotations

_SIGNUP_KEYWORDS = (
    "s'inscrire", "inscription", "créer un compte", "creer un compte",
    "créer votre compte", "creer votre compte",
    "sign up", "signup", "register", "create account", "create your account",
)

_FORGOT_PASSWORD_KEYWORDS = (
    "mot de passe oublié", "mot de passe oublie", "identifiant oublié",
    "identifiant oublie", "forgot password", "forgot your password",
    "trouble signing in", "besoin d'aide pour vous connecter",
    "besoin d'aide pour se connecter",
)

# Fournisseur d'identité tiers (SSO / OAuth) : mots-clés à repérer dans le
# libellé ET domaines de destination connus. Chaque signal (libellé OU
# domaine) est individuellement exploitable : un site peut afficher un
# domaine de redirection explicite sans nommer le fournisseur dans le
# texte visible, ou l'inverse.
_OAUTH_PROVIDERS: dict[str, dict[str, tuple[str, ...]]] = {
    "Google": {
        "label_hints": ("google",),
        "domain_hints": ("accounts.google.com", "google.com/o/oauth", "googleapis.com"),
    },
    "Microsoft": {
        "label_hints": ("microsoft", "outlook", "office 365", "azure ad"),
        "domain_hints": ("login.microsoftonline.com", "login.live.com"),
    },
    "Apple": {
        "label_hints": ("apple",),
        "domain_hints": ("appleid.apple.com",),
    },
    "Facebook": {
        "label_hints": ("facebook",),
        "domain_hints": ("facebook.com/dialog/oauth", "facebook.com/v", "facebook.com/login"),
    },
    "LinkedIn": {
        "label_hints": ("linkedin",),
        "domain_hints": ("linkedin.com/oauth", "linkedin.com/uas/login"),
    },
    "GitHub": {
        "label_hints": ("github",),
        "domain_hints": ("github.com/login/oauth",),
    },
}

# Un bouton/lien SSO se reconnaît par un verbe de connexion générique
# ("continuer avec", "se connecter avec", "sign in with"...) COUPLÉ au nom
# d'un fournisseur (voir _OAUTH_PROVIDERS). Le verbe seul ne suffit pas
# ("Se connecter" tout court = login natif, pas SSO) : on exige la
# présence du nom du fournisseur, dans le libellé OU dans le domaine de
# destination.
_OAUTH_VERB_HINTS = (
    "continuer avec", "continue with", "se connecter avec", "se connecter via",
    "sign in with", "log in with", "connect with", "s'identifier avec",
    "connexion avec", "connexion via", "signing in with",
)


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def detect_oauth_provider(label: str | None, possible_destination: str | None) -> str | None:
    """
    Retourne le nom du fournisseur ("Google", "Microsoft"...) si ce
    libellé/cette destination correspond clairement à un bouton de
    connexion via un fournisseur d'identité tiers, sinon None.
    """
    label_n = _norm(label)
    dest_n = _norm(possible_destination)

    for provider, hints in _OAUTH_PROVIDERS.items():
        if any(h in dest_n for h in hints["domain_hints"]):
            # Le domaine de destination à lui seul est un signal fort et
            # suffisant (ex: redirection directe, libellé générique "Se
            # connecter" sans mention explicite du fournisseur dans le texte).
            return provider
        label_has_provider = any(h in label_n for h in hints["label_hints"])
        if label_has_provider and any(v in label_n for v in _OAUTH_VERB_HINTS):
            return provider
    return None


def detect_auth_kind(label: str | None, possible_destination: str | None = None) -> str | None:
    """
    Classe un élément UI (lien/bouton) lié à l'authentification mais
    DISTINCT du formulaire de connexion natif lui-même :
      - "oauth:<Provider>" -> bouton de connexion via un fournisseur tiers
      - "signup"           -> lien "Créer un compte" / "S'inscrire"
      - "forgot_password"  -> lien "Mot de passe oublié"
    Retourne None si l'élément ne correspond à aucun de ces cas — inclut
    délibérément le bouton/lien de connexion natif "Se connecter" : ce
    n'est PAS un cas "annexe", c'est le formulaire de connexion lui-même,
    qui doit continuer à recevoir les cas nominal/négatifs de
    test_planning_service._RULES (mauvais mot de passe, champ vide...).
    """
    provider = detect_oauth_provider(label, possible_destination)
    if provider:
        return f"oauth:{provider}"

    label_n = _norm(label)
    if any(kw in label_n for kw in _SIGNUP_KEYWORDS):
        return "signup"
    if any(kw in label_n for kw in _FORGOT_PASSWORD_KEYWORDS):
        return "forgot_password"
    return None


def is_auth_adjacent_link(label: str | None, possible_destination: str | None = None) -> bool:
    """True si detect_auth_kind() reconnaît ce libellé/cette destination
    comme un cas d'authentification ANNEXE (inscription, mot de passe
    oublié, SSO tiers) — utilisé par navigation_discovery_service pour ne
    JAMAIS classer ces liens comme navigation générique."""
    return detect_auth_kind(label, possible_destination) is not None
