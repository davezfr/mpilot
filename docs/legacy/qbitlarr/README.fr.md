# qBitlarr

**Langue :** [English](README.md) | [中文](README.zh-CN.md) | Français

**Une passerelle légère entre Prowlarr et qBittorrent, avec support REST, MCP et CLI.**

qBitlarr s'adresse aux personnes qui utilisent déjà Plex, Jellyfin ou Emby et qui veulent permettre à des amis, à la famille ou à un agent LLM de demander des films et des séries sans leur donner accès à qBittorrent et sans installer toute la pile Sonarr + Radarr.

qBitlarr est un petit service FastAPI qui peut :

- Recevoir un titre en langage naturel, un identifiant IMDb, ou un lien IMDb / Douban / AlloCine.
- Identifier le titre, puis chercher dans vos indexeurs Prowlarr.
- Classer les releases selon des préférences de qualité configurables et vous montrer les meilleures options.
- Ajouter votre choix à votre qBittorrent existant, ou choisir automatiquement la meilleure release en mode `auto`.
- Exposer la même logique via REST, MCP et une petite CLI, afin de s'intégrer à Claude Desktop, Cursor, ChatGPT custom tools, des bots Telegram, des scripts shell, des cron jobs ou vos propres agents.

Fonctionne avec n'importe quel client HTTP, Claude/Cursor/ChatGPT via MCP, ou la CLI `qbitlarr`.

## Architecture

![qBitlarr architecture: a friend, family member, shell script, or LLM agent talks REST, MCP, or CLI to the qbitlarr FastAPI service, which uses Prowlarr and FlareSolverr to search torrent indexers and then drives your own qBittorrent Web UI, which saves files into your Plex/Jellyfin/Emby library.](docs/architecture.png)

Source modifiable du diagramme REST / MCP / CLI : [docs/architecture.svg](docs/architecture.svg).

## Ce que Docker Compose lance

- `qbitlarr` — le service FastAPI sur `http://localhost:8000`
- `prowlarr` — Prowlarr inclus sur `http://localhost:9696`
- `flaresolverr` — FlareSolverr inclus sur `http://localhost:8191`

qBittorrent **n'est pas** inclus. Pointez qBitlarr vers un qBittorrent existant — application de bureau, NAS, seedbox, conteneur séparé — avec `QBIT_URL`, `QBIT_USERNAME` et `QBIT_PASSWORD`.

## Configuration de qBittorrent

qBitlarr a besoin d'une installation qBittorrent existante, parce que chaque personne organise ses téléchargements et ses chemins de médiathèque différemment : application de bureau, NAS, seedbox ou conteneur séparé. qBitlarr parle uniquement à qBittorrent via son API Web UI.

Avant de lancer qBitlarr :

1. Installez qBittorrent là où vos téléchargements doivent s'exécuter.
2. Dans qBittorrent, ouvrez **Preferences / Options → Web UI** et activez la Web User Interface.
3. Définissez ou vérifiez le nom d'utilisateur et le mot de passe de la Web UI.
4. Renseignez ces valeurs dans `.env` :

```sh
QBIT_URL=http://host.docker.internal:8080
QBIT_USERNAME=your-webui-username
QBIT_PASSWORD=your-webui-password
```

Utilisez `http://host.docker.internal:8080` quand qBittorrent tourne sur la même machine que Docker Compose. Si qBittorrent tourne sur un NAS, une seedbox ou un autre ordinateur, utilisez plutôt l'URL LAN de cette machine, par exemple `http://192.168.1.50:8080`. N'utilisez pas `localhost` dans `.env` pour un qBittorrent installé sur l'hôte ; depuis Docker, `localhost` désigne le conteneur qBitlarr lui-même.

## Démarrage rapide

```sh
cp .env.example .env
# éditez .env : renseignez QBIT_URL, QBIT_USERNAME et QBIT_PASSWORD depuis votre qBittorrent Web UI

# 1. Lancez d'abord Prowlarr pour récupérer sa clé API
docker compose up -d prowlarr flaresolverr

# 2. Ouvrez http://localhost:9696, terminez la configuration initiale, ajoutez des indexeurs,
#    puis copiez la clé API depuis Settings -> General -> Security
# 3. Placez la clé dans .env avec PROWLARR_API_KEY

# 4. Lancez le reste
docker compose up -d --build

# 5. Testez
curl -X POST http://localhost:8000/handle \
  -H 'Content-Type: application/json' \
  -d '{"user_message":"tt0045877"}'
```

Pour vérifier aussi que Prowlarr et qBittorrent sont joignables :

```sh
curl 'http://localhost:8000/health?deep=true'
```

## À quoi ça ressemble

Une fois qBitlarr branché à votre agent (ou via la CLI), vous lui parlez comme à un ami qui connaît votre configuration média :

Pour les demandes de films, qBitlarr accepte directement les liens et IDs IMDb. Il peut aussi résoudre les liens ou IDs de films Douban et AlloCine pris en charge vers le même flux basé sur IMDb. Si un film Douban ou AlloCine ne peut pas être résolu de manière fiable, qBitlarr demande IMDb plutôt que de deviner.

Les exemples ci-dessous utilisent [The Hitch-Hiker (1953)](https://www.imdb.com/title/tt0045877/), un film du domaine public listé par la Library of Congress dans son ensemble [Public Domain Films from the National Film Registry](https://www.loc.gov/free-to-use/public-domain-films-from-the-national-film-registry/). Les droits peuvent quand même varier selon la juridiction et selon la restauration, la bande-son, les sous-titres ou l'édition précise.

<table>
  <tr>
    <td width="42.5%" align="center" valign="middle">
      <img src="docs/screenshots/telegram-imdb-release-picker.jpg" height="430" alt="Exemple Telegram : partager la page IMDb de The Hitch-Hiker, choisir une release, puis suivre la progression du téléchargement qBitlarr.">
    </td>
    <td width="57.5%" align="center" valign="middle">
      <img src="docs/screenshots/telegram-title-release-picker.jpg" height="430" alt="Exemple Telegram : rechercher par titre, choisir le bon film, choisir une release, puis suivre la progression du téléchargement qBitlarr.">
    </td>
  </tr>
</table>

*Captures d'écran fournies à titre d'exemple uniquement. À gauche, un lien ou ID IMDb mène directement au choix de release et au suivi du téléchargement. À droite, la recherche `The Hitchhiker 1953` affiche d'abord quatre choix de titre ; sélectionner The Hitch-Hiker (1953) ouvre le même sélecteur de releases et la même vue de progression. Le titre de démonstration est un exemple Public Domain ; les droits peuvent varier selon la juridiction, la restauration, la bande-son, les sous-titres ou l'édition précise.*

> **Vous :** *Télécharge The Hitchhiker 1953.*
> **Agent :** Quel titre voulez-vous ?
>   1. The Hitchhiker's Guide to the Galaxy (2005)
>   2. The Hitch-Hiker (1953)
>   3. An American Hippie in Israel (1972)
>   4. The Hitch Hiker (2004)

> **Vous :** *Touchez The Hitch-Hiker (1953).*
> **Agent :** Choisissez la version à télécharger :
>   1. WEB-DL · H.264 · 5.3 GB
>   2. Option 1.2 GB
>   3. WEB-DL · H.264 · 5.3 GB
>   4. BluRay · H.264 · 5 GB
>   5. BluRay · H.264 · 8.7 GB

> **Vous :** *Touchez la release de 1.2 GB.*
> **Agent :** C'est bon, je lance le téléchargement.
> **Agent :** ⬇️ The Hitch-Hiker (1953) [1080p]<br>
> 🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 36%<br>
> 💾 421.9 MB / 1.1 GB<br>
> ⚡ Speed: 558.4 KB/s<br>
> ⏱️ ETA: 5m 24s

> **Vous :** *Télécharge tt0045877*
> **Agent :** Comme l'ID IMDb identifie déjà le film, qBitlarr saute le choix du titre et ouvre directement les boutons de release.

En coulisses, chaque demande est d'abord résolue vers un titre précis : un lien ou ID IMDb / Douban / AlloCine verrouille le titre directement, tandis qu'un mot-clé est comparé via Wikidata. Si plusieurs titres correspondent, qBitlarr renvoie des choix de titre que les adaptateurs de chat peuvent afficher comme boutons ; en sélectionner un continue vers le sélecteur de releases. Si rien ne correspond, qBitlarr demande un lien IMDb plutôt que de deviner. Une fois le titre fixé, il classe les releases et renvoie les meilleures options sous forme de boutons et tableaux structurés ; en mode `auto`, il ajoute directement la meilleure. Vous pouvez toujours dire *"4K"*, *"Remux"* ou *"720p HEVC"* pour remplacer les préférences par défaut. Le statut peut revenir sous forme de données brutes (`qbitlarr_list_downloads` / `qbitlarr_get_download_status`) ou de cartes de progression emoji prêtes pour le chat (`qbitlarr_render_*`) ; voir [Connecter un agent](#connecter-un-agent) pour les détails de rafraîchissement et de notifications de fin.

### Astuce : partager directement depuis l'app IMDb

Le moyen le plus rapide d'utiliser qBitlarr, c'est de ne rien taper du tout :

1. Dans l'app IMDb (ou n'importe quel site qui affiche une URL IMDb), trouvez ce que vous voulez.
2. Appuyez sur l'icône de partage → choisissez l'app où vit votre agent (Telegram, WhatsApp, Discord, Signal, iMessage, etc.).
3. L'agent reçoit une URL du type `https://www.imdb.com/title/tt0045877/` et identifie le titre tout seul — pas de saisie, pas de fautes d'orthographe, pas d'ambiguïté.

Un identifiant IMDb brut comme `tt0045877` marche pareil si vous en avez un sous la main. qBitlarr accepte aussi `douban:1292052` et `allocine:25801` pour les IDs de films pris en charge. Il résout ces IDs et va directement aux choix de releases pour ce titre exact, sans étape de correspondance de titre.

## Quand l'utiliser plutôt que Sonarr / Radarr

Utilisez **Sonarr/Radarr** si vous voulez un gestionnaire de médiathèque complet : suivi des épisodes, politiques de mise à niveau, surveillance automatique des nouvelles sorties, profils de qualité très détaillés.

Utilisez **qBitlarr** si vous voulez simplement : *"un ami donne le nom d'un film → il apparaît dans Plex une heure plus tard."* Pas de médiathèque, pas de surveillance, pas d'interface de profils. Un service, quelques variables d'environnement, et c'est tout.

## Utilisation responsable

qBitlarr est une passerelle d'automatisation. Il ne fournit pas de contenu, d'indexeurs, de trackers ni de conseil juridique. Utilisez-le uniquement avec des indexeurs et des médias auxquels vous avez le droit d'accéder dans votre juridiction.

## Configurer les indexeurs dans Prowlarr

Si vous découvrez **Prowlarr** : c'est un *agrégateur d'indexeurs*. Il se connecte à plusieurs sites de torrents, appelés indexeurs, et donne à qBitlarr une seule API de recherche. Sans lui, qBitlarr devrait connaître les détails de dizaines de sites différents. Vous ajoutez les indexeurs une fois dans Prowlarr, puis chaque recherche qBitlarr les interroge en parallèle.

**Ajouter un indexeur :**

1. Ouvrez `http://localhost:9696` puis allez dans **Indexers → + Add Indexer**.
2. Tapez le nom de l'indexeur dans le filtre.
3. **Indexeur public** : en général, cliquez simplement sur **Save**. Aucun compte n'est nécessaire.
4. **Tracker privé** : collez le cookie, la clé API ou le passkey de votre compte sur ce tracker. Les champs varient selon les trackers, et le formulaire Prowlarr indique ce qui est requis.
5. Cliquez sur **Test** pour vérifier que Prowlarr peut l'atteindre, puis sur **Save**.
6. L'indexeur possède maintenant un ID numérique, visible avec `curl http://localhost:8000/prowlarr/indexers`.

Pour les indexeurs derrière Cloudflare, ajoutez aussi le proxy tag `flaresolverr`. Voir [Pourquoi FlareSolverr est inclus](#pourquoi-flaresolverr-est-inclus).

**Indexeurs publics vs trackers privés :**

- **Indexeurs publics** : souvent rapides à ajouter, mais les résultats sont plus bruités : plus de torrents morts, de spam et de fausses releases.
- **Trackers privés** : nécessitent un compte et ont souvent des règles d'accès plus strictes. Les champs de configuration varient ; suivez les exigences des trackers que vous êtes autorisé à utiliser.

**Recommandations :**

- **Commencez avec 2 à 4 indexeurs, pas 20.** Chaque indexeur ajoute de la latence à chaque recherche. Un site lent peut ralentir toute la requête, et empiler des indexeurs publics empile souvent du bruit plutôt que de la qualité.
- **Mélangez couverture et qualité.** Un ou deux indexeurs publics généralistes comme filet de sécurité, plus les trackers privés auxquels vous avez accès, donnent une bonne base.
- **Ignorez `Sync Profiles`** sauf si vous utilisez aussi Sonarr ou Radarr. qBitlarr n'en a pas besoin.

Une fois les indexeurs configurés, vous pouvez définir des IDs primary et fallback dans [Sélection des indexeurs](#sélection-des-indexeurs). qBitlarr cherchera d'abord dans vos indexeurs rapides et fiables, puis ne basculera vers les sources plus larges ou plus lentes que si nécessaire.

## Pourquoi FlareSolverr est inclus

Certains indexeurs populaires sont protégés par le **challenge anti-bot de Cloudflare**. Une requête HTTP simple — celle que Prowlarr envoie par défaut — reçoit une page de challenge HTML au lieu de résultats de recherche. L'indexeur semble alors ne rien renvoyer.

**FlareSolverr** est un petit proxy basé sur Chrome headless qui résout ces challenges pour Prowlarr. Quand Prowlarr est configuré pour faire passer certains indexeurs par lui, FlareSolverr ouvre la page dans un vrai navigateur, attend que Cloudflare valide la session, puis renvoie les cookies à Prowlarr pour que la recherche fonctionne.

qBitlarr l'inclut parce qu'un utilisateur qui ajoute un indexeur protégé par Cloudflare dans Prowlarr rencontre vite ce blocage, et la solution officielle revient souvent à installer FlareSolverr séparément. Le fournir dans le compose évite cette friction.

**Le connecter dans Prowlarr** une fois le premier démarrage terminé :

1. Ouvrez Prowlarr sur `http://localhost:9696`.
2. Allez dans **Settings → Indexers → Indexer Proxies**.
3. Cliquez sur **+** puis choisissez **FlareSolverr**.
4. Définissez **Host** à `http://flaresolverr:8191`, le hostname interne du compose, et donnez-lui un **Tag** comme `flaresolverr`.
5. Sauvegardez. Ensuite, pour chaque indexeur protégé par Cloudflare, ouvrez sa configuration, ajoutez ce même tag `flaresolverr`, puis sauvegardez.

Les indexeurs sans ce tag ne passent pas par FlareSolverr, donc il n'y a pas de coût pour les sites non protégés. Si vous n'utilisez aucun indexeur protégé par Cloudflare, vous pouvez arrêter le conteneur avec `docker compose stop flaresolverr` et qBitlarr continuera à fonctionner.

## Préférences de qualité

Par défaut, qBitlarr vise **1080p WEB-DL H.264** avec au moins 5 seeders. Vous pouvez changer les valeurs par défaut avec :

```sh
QBITLARR_PREFER_RESOLUTION=1080p   # 480p | 720p | 1080p | 2160p
QBITLARR_PREFER_SOURCE=WEB-DL      # WEB-DL | WEBRip | BluRay | HDTV
QBITLARR_PREFER_CODEC=H.264        # H.264 | H.265
QBITLARR_MIN_SEEDERS=5
```

Les utilisateurs peuvent aussi remplacer ces préférences dans chaque demande en langage naturel :

- `"The Hitch-Hiker 4K"` → force 2160p
- `"The Hitch-Hiker Remux"` → force une release Remux
- `"The Hitch-Hiker 720p HEVC"` → 720p H.265

## Comment une demande est résolue

Chaque requête `/handle` suit le même chemin, donc un mot-clé et un lien IMDb finissent au même endroit :

1. **Identifier le titre.** Un ID/URL IMDb ou un lien Douban/AlloCine pris en charge est résolu directement. Un mot-clé est comparé via Wikidata (sans clé API ni compte supplémentaire). Si plusieurs titres correspondent, qBitlarr renvoie une liste `choose_title` (titre + année) et attend le choix de l'utilisateur ; si rien ne correspond, il renvoie `needs_imdb` et demande un lien IMDb.
2. **Classer les releases** pour ce titre unique selon vos préférences de qualité.
3. **Renvoyer les 4 meilleures releases par défaut** à choisir, ou ajouter directement la meilleure en mode `auto`.

La correspondance par mot-clé via Wikidata est volontairement légère ; certains titres obscurs peuvent ne pas être résolus. Dans ce cas, qBitlarr demande un lien IMDb au lieu de deviner.

### Modes de sortie

`POST /handle` accepte un champ optionnel `mode` :

- `manual` *(par défaut)* — renvoie des choix de releases classés et n'ajoute rien à la file.
- `auto` — ajoute directement la meilleure release. Idéal pour un usage simple par des amis ou la famille ; la réponse inclut une liste `alternatives` de 2 à 3 options pour proposer "ou vouliez-vous plutôt...".
- `confirm` — renvoie le meilleur choix et quelques alternatives, mais n'ajoute rien à la file.

Changez le mode serveur par défaut avec `QBITLARR_DEFAULT_MODE=manual|auto|confirm`.

L'affichage des choix reste neutre côté transport pour la désambiguïsation de titre (`choose_title`) et le choix de release (`show_results`). La réponse REST inclut des `label` compacts pour les outils clarify/picker génériques, ainsi que des champs de choix rendus pour les adaptateurs de chat plus riches. `choice_rich_message` est du rich HTML compatible Telegram Bot API 10.1 : un adaptateur peut passer sa valeur `html` comme `sendRichMessage.rich_message.html`, puis rendre `choice_buttons` dessous. Si les messages rich ne sont pas disponibles, envoyez `choice_display` seul, sans ajouter `choices_table`, `results` ni `label`. Le wrapper MCP renvoie plutôt un objet `agent_clarify` : les flows de style Hermes doivent placer `agent_clarify.display_table` dans un bloc fenced text/code, ajouter `agent_clarify.display_notice` après le bloc quand il est présent, passer `agent_clarify.choices` comme libellés courts de boutons numériques, puis mapper le numéro choisi via `agent_clarify.response_mapping`. Le défaut zéro configuration des releases est compatible avec Hermes stock : `QBITLARR_MANUAL_RESULT_LIMIT=4` et `QBITLARR_CHOICE_STYLE=hermes-default`, ce qui correspond aux surfaces clarify de style Hermes qui affichent quatre lignes sans listes numérotées dupliquées. Si votre adaptateur local Telegram/Hermes sait rendre une table rich plus une ligne fermée de cinq boutons, définissez :

```sh
QBITLARR_MANUAL_RESULT_LIMIT=5
QBITLARR_CHOICE_STYLE=telegram-rich
```

Cela modifie uniquement la réponse structurée de qBitlarr ; la mise en page horizontale des boutons reste dans votre adaptateur de chat local ou votre profil Hermes. En mode `telegram-rich`, qBitlarr omet le `choices_table` brut et renvoie un fallback `choice_display` en texte brut, sans Markdown fence, afin que les bots Telegram n'affichent pas de blocs de code ni de listes numérotées en double.

## Nettoyage des tâches terminées

qBitlarr peut supprimer périodiquement les tâches qBittorrent terminées qu'il gère, tout en conservant les fichiers téléchargés. Les nouveaux téléchargements qBitlarr reçoivent le tag `qbitlarr.managed` ; les anciens tags `requester.*` peuvent aussi être inclus par compatibilité.

Désactivé par défaut. Activez et ajustez avec ces variables :

```sh
QBITLARR_CLEANUP_ENABLED=false
QBITLARR_CLEANUP_COMPLETED_AFTER_SECONDS=259200
QBITLARR_CLEANUP_INTERVAL_SECONDS=21600
QBITLARR_CLEANUP_INCLUDE_LEGACY_REQUESTER_TAGS=true
```

Notes :

- `QBITLARR_CLEANUP_COMPLETED_AFTER_SECONDS=259200` nettoie les tâches terminées depuis au moins 3 jours.
- `QBITLARR_CLEANUP_INTERVAL_SECONDS=21600` vérifie toutes les 6 heures.
- Le nettoyage appelle qBittorrent avec `delete_files=false`, donc il supprime seulement la tâche, pas les fichiers média.
- Les torrents non gérés, sans tag `qbitlarr.managed` ni ancien tag `requester.*`, sont ignorés.

Les query snapshots utilisés par la sélection manuelle des résultats sont prunés indépendamment du nettoyage des tâches qBittorrent. La boucle de maintenance prune les snapshots même lorsque `QBITLARR_CLEANUP_ENABLED=false` ; ajustez la rétention de 7 jours par défaut avec :

```sh
QBITLARR_QUERY_SNAPSHOT_RETENTION_SECONDS=604800
```

## Connecter un agent

qBitlarr est livré comme un **serveur MCP**, donc n'importe quel agent qui parle le [Model Context Protocol](https://modelcontextprotocol.io) — Claude Desktop, Cursor, Cline, Hermes, OpenClaw, ChatGPT via un bridge MCP, votre propre agent maison — peut l'utiliser.

Les outils MCP sont neutres côté langue. Vous pouvez poser la question en anglais, chinois, français ou toute autre langue que le LLM de votre agent sait gérer ; l'agent peut répondre dans la même langue. Ce comportement multilingue dépend du LLM derrière votre agent, pas de qBitlarr lui-même.

Deux transports sont disponibles :

- **stdio MCP** — ce que la plupart des applications agent de bureau préfèrent. Elles lancent `bin/qbitlarr-mcp` comme sous-processus.
- **HTTP MCP** — exposé sur `http://localhost:8000/mcp` pour les hosts qui préfèrent HTTP.

Outils exposés par les deux transports : `qbitlarr_handle`, `qbitlarr_search`, `qbitlarr_download`, `qbitlarr_list_downloads`, `qbitlarr_get_download_status`, `qbitlarr_render_downloads_status`, `qbitlarr_render_download_status`, `qbitlarr_pause_download`, `qbitlarr_resume_download`, `qbitlarr_delete_download`, `qbitlarr_watch_download`, `qbitlarr_get_query_snapshot`, `qbitlarr_list_prowlarr_indexers`, `qbitlarr_health`.

Le wrapper MCP stdio peut aussi envoyer des **notifications de fin uniques** vers des cibles de style Hermes :

- Passez `notification_target` (par exemple `telegram:123456789`) lors de l'ajout d'un torrent. qBitlarr surveille le hash, publie un message de progression, le rafraîchit selon l'intervalle de watch, puis envoie un message à cette cible à 100 %. Si `user_id` / `requester_id` est déjà une cible Hermes, elle est réutilisée automatiquement ; les bots multi-utilisateurs ont rarement besoin de passer `notification_target` séparément.
- Le même `user_id` / `requester_id` par utilisateur limite les vérifications de statut aux torrents tagués pour cet utilisateur.
- Pour les flux manuels, appelez `qbitlarr_watch_download` avec un hash connu ; passez `completion_followup_message` pour ajouter une ligne indiquant ce qui commence ensuite, par exemple le traitement des sous-titres.
- L'édition de progression Telegram lit `QBITLARR_TELEGRAM_BOT_TOKEN`, puis `QBITLARR_HERMES_ENV_PATH`, `HERMES_HOME/.env`, `~/.hermes/.env` ; avec plusieurs bots, pointez `QBITLARR_HERMES_ENV_PATH` vers le `.env` du profil concerné.
- L'état de watch utilise `QBITLARR_NOTIFICATION_WATCHES_PATH` s'il est défini ; sinon il va dans `$XDG_DATA_HOME/qbitlarr/download-notification-watches.json`, ou `~/.local/share/qbitlarr/download-notification-watches.json` lorsque `XDG_DATA_HOME` n'est pas défini.
- `QBITLARR_COMPLETION_HOOK_COMMAND` lance une commande locale après la fin ou la suppression d'un téléchargement surveillé ; qBitlarr envoie d'abord le message utilisateur, puis écrit un événement JSON `download_complete` / `download_removed` sur stdin. Les échecs de hook sont retentés sans masquer la notification utilisateur.

Si `QBITLARR_API_KEY` est défini, les deux transports exigent un header `X-API-Key`. Le MCP stdio lit la même variable d'environnement.

### Claude Desktop

Éditez `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) ou `%APPDATA%\Claude\claude_desktop_config.json` (Windows) :

```json
{
  "mcpServers": {
    "qbitlarr": {
      "command": "/absolute/path/to/qbitlarr/bin/qbitlarr-mcp",
      "env": {
        "QBITLARR_API_URL": "http://localhost:8000",
        "QBITLARR_API_KEY": ""
      }
    }
  }
}
```

Redémarrez Claude Desktop. Les outils qbitlarr apparaissent dans la liste et Claude les utilise quand vous parlez de films ou de séries.

### Cursor

Settings → **MCP** → **Add new MCP server** :

```json
{
  "mcpServers": {
    "qbitlarr": {
      "command": "/absolute/path/to/qbitlarr/bin/qbitlarr-mcp"
    }
  }
}
```

### N'importe quel autre host MCP (Hermes, OpenClaw, Cline, agents personnalisés)

Le schéma est identique — tous supportent l'un ou l'autre, voire les deux transports :

- **Voie stdio** : configurez le host pour lancer `bin/qbitlarr-mcp` comme sous-processus (avec les variables d'environnement pour l'URL de l'API et la clé optionnelle).
- **Voie HTTP** : pointez le host vers `http://localhost:8000/mcp`, en ajoutant le header `X-API-Key` si vous en avez défini un.

Pour `choose_title` et `show_results`, les hosts MCP doivent poser une question picker avec `agent_clarify.display_table` dans un bloc monospace, ajouter `agent_clarify.display_notice` après le bloc quand il est présent, passer `agent_clarify.choices` comme libellés courts de boutons numériques, puis mapper le numéro choisi via `agent_clarify.response_mapping`. Les adaptateurs Telegram REST qui prennent en charge Bot API `sendRichMessage` doivent rendre `choice_rich_message` d'abord, puis placer `choice_buttons` dessous. Si ce n'est pas disponible, envoyez `choice_display` seul. Les hosts texte brut utilisant la réponse REST `hermes-default` peuvent afficher `choices_table` dans un bloc monospace.

### Indiquer à l'agent quand utiliser qBitlarr

Si votre agent expose un system prompt ou un champ "tool instructions", ajoutez une courte indication pour qu'il pense à qBitlarr au bon moment :

> *Quand l'utilisateur demande à télécharger un film, une série ou un anime auquel il est autorisé à accéder, utilise les outils MCP qbitlarr. Par défaut, appelle `qbitlarr_handle` — il accepte les IDs IMDb, les URLs IMDb, les liens ou IDs de films Douban pris en charge, les liens ou IDs de films AlloCine pris en charge, et les titres en texte libre. Par défaut, il renvoie des choix de releases classés ; si un mot-clé correspond à plusieurs titres, il renvoie d'abord un court sélecteur de titres ; s'il renvoie `needs_imdb`, demande à l'utilisateur un lien IMDb. Ne reviens à `qbitlarr_search` + `qbitlarr_download` que pour un contrôle manuel avancé.*

Cela aide les agents qui ne savaient pas que vous aviez un downloader connecté.

### Vérification rapide

Une fois branché, demandez à l'agent : *"Utilise qbitlarr_health pour vérifier que le service tourne."* S'il renvoie `{"status": "ok"}`, c'est connecté. Ajoutez `--deep` (ou passez `deep: true`) pour vérifier aussi que Prowlarr et qBittorrent répondent.

## CLI

La CLI est un client léger pour la même API REST que celle utilisée par MCP. Elle lit `QBITLARR_API_URL`, `QBITLARR_API_KEY` et `QBITLARR_API_TIMEOUT_SECONDS` depuis l'environnement, avec des flags disponibles pour les surcharger.

`handle` affiche par défaut une réponse lisible par un humain. Ajoutez `--json` pour obtenir la réponse structurée brute. Les autres sous-commandes affichent du JSON par défaut pour être utilisées avec `jq`.

```sh
bin/qbitlarr handle "tt0045877"
bin/qbitlarr handle "douban:1292052"
bin/qbitlarr handle "https://www.allocine.fr/film/fichefilm_gen_cfilm=25801.html"
bin/qbitlarr handle "The Hitch-Hiker" --mode manual
bin/qbitlarr handle "The Hitch-Hiker" --user-id telegram:123456789
bin/qbitlarr handle "The Hitch-Hiker" --mode manual --json
bin/qbitlarr search --query "The Hitch-Hiker 1953 1080p" | jq '.[0]'
bin/qbitlarr download 'magnet:?xt=urn:btih:...' --user-id telegram:123456789
bin/qbitlarr downloads --watch --user-id telegram:123456789
bin/qbitlarr downloads --render --user-id telegram:123456789
bin/qbitlarr download-status abcdef1234567890 --user-id telegram:123456789
bin/qbitlarr download-status abcdef1234567890 --render --user-id telegram:123456789
bin/qbitlarr health --deep
bin/qbitlarr indexers
```

Mettez les liens magnet entre guillemets dans votre shell, car ils contiennent souvent `&`.

Dans le conteneur Docker, lancez le même module CLI avec `docker compose exec qbitlarr python -m app.cli health --deep`. Le launcher `bin/qbitlarr` est destiné à l'utilisation depuis le checkout sur l'hôte.

## Authentification

Pour un déploiement au-delà de localhost, définissez `QBITLARR_API_KEY`. Chaque requête REST et MCP devra alors fournir le header `X-API-Key` :

```sh
curl -H 'X-API-Key: change-this' http://localhost:8000/health
```

Laissez vide pour un usage local sans authentification.

## URLs Prowlarr

`PROWLARR_URL` est l'URL utilisée par qBitlarr pour appeler l'API Prowlarr. Dans Docker Compose, elle vaut par défaut `http://prowlarr:9696`, le hostname interne du service. La plupart des utilisateurs n'ont pas besoin de la modifier.

`PROWLARR_DOWNLOAD_URL` est optionnel. Définissez-le seulement lorsque Prowlarr renvoie des URLs de téléchargement proxy que qBitlarr doit réécrire avant de récupérer le fichier `.torrent`, par exemple si qBitlarr doit joindre Prowlarr via une adresse LAN plutôt que par le hostname Docker interne.

## Sélection des indexeurs

`PROWLARR_PRIMARY_INDEXER_IDS` et `PROWLARR_FALLBACK_INDEXER_IDS` sont des listes optionnelles d'IDs d'indexeurs séparés par des virgules.

- Laissez les deux vides pour laisser Prowlarr chercher dans tous les indexeurs applicables.
- Définissez des IDs primary pour privilégier un sous-ensemble fiable.
- Définissez des IDs fallback pour essayer des indexeurs plus larges ou plus lents seulement lorsque les résultats primary sont absents ou inadaptés.

Découvrez les IDs une fois Prowlarr configuré :

```sh
curl http://localhost:8000/prowlarr/indexers
```

## Chemins de sauvegarde

`/handle` choisit un chemin de sauvegarde pour chaque téléchargement ajouté à la file selon le type de média et la résolution :

- `QBITLARR_SAVE_PATH_MOVIE=/downloads/movies`
- `QBITLARR_SAVE_PATH_MOVIE_4K=/downloads/movies-4k`
- `QBITLARR_SAVE_PATH_TV=/downloads/tv`

Les téléchargements de séries créent un dossier par série sous le chemin TV de base, par exemple `/downloads/tv/Example Show`.

`/handle` et `/download` acceptent aussi un champ optionnel `save_path` pour les remplacements ponctuels. Ces chemins doivent se trouver sous l'une des racines configurées ci-dessus, ou sous une entrée de `QBITLARR_EXTRA_SAVE_PATHS` séparée par des virgules, par exemple `/media/Kids`.

Quand `save_path` est omis, `/handle` et `/download` utilisent les chemins par défaut configurés dans qBitlarr. `/download` déduit la destination depuis les métadonnées du torrent ou le display name du magnet, afin que les sélections manuelles issues des résultats de recherche arrivent aussi dans le chemin film, film 4K ou série, plutôt que dans le dossier global par défaut de qBittorrent.

## API REST

| Method | Path | Objectif |
| --- | --- | --- |
| GET | `/health` | Vérification de vie du service |
| GET | `/health?deep=true` | Vie du service + accessibilité Prowlarr/qBittorrent |
| POST | `/handle` | Point d'entrée principal : chercher et éventuellement ajouter à la file |
| POST | `/search` | Recherche Prowlarr brute |
| POST | `/download` | Ajouter un lien de téléchargement connu |
| GET | `/downloads` | Lister les torrents dans qBittorrent |
| GET | `/downloads/status-message` | Rendre les téléchargements comme message de progression pour le chat |
| GET | `/downloads/{info_hash}` | Lire un torrent par info hash |
| GET | `/downloads/{info_hash}/status-message` | Rendre un torrent comme message de progression pour le chat |
| POST | `/downloads/{info_hash}/pause` | Mettre en pause un torrent appartenant au requester |
| POST | `/downloads/{info_hash}/resume` | Reprendre un torrent appartenant au requester |
| POST | `/downloads/{info_hash}/delete` | Supprimer une tâche qBittorrent du requester sans supprimer les fichiers |
| GET | `/queries/{query_id}` | Relire un snapshot de recherche sauvegardé |
| GET | `/prowlarr/indexers` | Lister les indexeurs Prowlarr avec leurs IDs |

Exemple : ajouter un lien connu dans un dossier précis.

```sh
curl -X POST http://localhost:8000/download \
  -H 'Content-Type: application/json' \
  -d '{"download_link":"magnet:?xt=urn:btih:...","save_path":"/media/Kids"}'
```

## Structure du projet

```
qbitlarr/
├── app/            Service FastAPI — API REST, CLI, et logique canonique
│   ├── api/        Handlers REST (handle, search, download, ...)
│   ├── domain/     Logique pure : classement, chemins, tables de choix, cartes de progression
│   └── services/   Clients externes : prowlarr, qbittorrent, wikidata
├── mcp_server/     Serveur MCP stdio (wrappers fins autour de app/client.py)
├── bin/            Launchers `qbitlarr` et `qbitlarr-mcp`
├── tests/          Suite pytest
├── docs/           Diagramme d'architecture + captures README
└── docker-compose.yml, Dockerfile, .env.example, README*.md
```

L'API REST est la surface canonique ; la CLI et le MCP stdio sont des clients fins de `app/client.py`. L'essentiel de la logique vit dans `app/api/handle.py` (orchestration : identifier → classer → ajouter à la file) et `app/domain/quality.py` (classement pur, sans réseau).

## Pair With Babelarr For Subtitles

qBitlarr gère l'acquisition ; associez-le à [Babelarr](https://github.com/davezfr/babelarr) pour préparer les sous-titres après la fin d'un téléchargement. Quand les deux serveurs MCP sont disponibles pour un même agent, *"Download The Hitch-Hiker and add Chinese-English subtitles"* devient : qBitlarr met le film en file, puis dès qu'un chemin local existe Babelarr trouve ou télécharge un sous-titre source, le traduit, et écrit le sidecar SRT/ASS. Pour une file plus durable, exposez aussi le Runtime MCP de Babelarr : il mémorise le téléchargement et déclenche Babelarr quand le chemin est prêt.

<p>
  <img src="docs/screenshots/telegram-qbitlarr-babelarr-one-shot.jpg" alt="Exemple Telegram : une seule demande télécharge His Girl Friday avec qBitlarr puis prépare des sous-titres chinois-anglais avec Babelarr.">
</p>

*Capture du workflow combiné fournie à titre d'exemple uniquement. La démo utilise un titre du domaine public ; les droits peuvent varier selon la juridiction, la restauration, la bande-son, les sous-titres ou l'édition précise.*

## Projets tiers

qBitlarr s'intègre avec ces projets tiers :

- **[Prowlarr](https://github.com/Prowlarr/Prowlarr)** — GPL-3.0. qBitlarr peut lancer Prowlarr comme service Docker Compose séparé et communique avec lui via son API HTTP.
- **[qBittorrent](https://github.com/qbittorrent/qBittorrent)** — GPL-2.0. qBitlarr attend que vous fournissiez qBittorrent séparément et communique avec lui via son API Web UI.
- **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)** — MIT. La configuration Docker Compose de qBitlarr l'inclut comme proxy de challenge optionnel pour les indexeurs Prowlarr qui en ont besoin.

qBitlarr n'est pas affilié à Prowlarr, qBittorrent, FlareSolverr ou leurs mainteneurs, et n'est ni approuvé ni sponsorisé par eux.

## License

MIT.
