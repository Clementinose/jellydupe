# JellyDupe

Hittar dubbletter i ditt Jellyfin-bibliotek och låter dig radera dem — med upplösning,
kodek, bitrate, filstorlek och sökväg synligt för varje fil innan du bestämmer dig.

Appen behöver inte se dina diskar. Den läser biblioteket via Jellyfins API och raderar
via samma API, så den fungerar även när Jellyfin (LXC 125) och medievolymen (Hugoria)
sitter på olika maskiner.

## Så hittas dubbletter

- **Filmer** grupperas på TMDB/IMDB-id. Två poster som pekar på samma film hamnar i
  samma grupp även om mapparna heter olika. Saknas provider-id används normaliserad
  titel + årtal, så "The Matrix" och "Matrix, The" matchar.
- **Avsnitt** grupperas på serie + säsong + avsnittsnummer.
- Både sammanslagna versioner (flera `MediaSources` på ett item) och separata items
  räknas som dubbletter.
- Filen med högst upplösning, sedan bitrate, sedan storlek märks **Bästa kvalitet**.
  Inget är förvalt — "Markera alla utom bästa" finns per grupp om du vill gå snabbt.

## Installera snabbast möjligt

### Via Portainer (rekommenderat)

1. **Gå till Portainer → Stacks → Add stack**
2. **Klistra in det här och klicka Deploy:**

```yaml
services:
  jellydupe:
    image: ghcr.io/clements/jellydupe:latest
    container_name: jellydupe
    ports:
      - "8095:8095"
    volumes:
      - jellydupe_config:/config
    restart: unless-stopped

volumes:
  jellydupe_config:
```

3. **Öppna** `http://portainer-machine:8095`

---

### Via Docker Compose (lokalt eller Unraid)

```bash
cd /någonstans/jellydupe
docker compose up -d
```

Öppna sedan `http://localhost:8095` eller `http://hugoria:8095`

---

## Första gången du öppnar appen

### 1. Anslut till Jellyfin

Första gången frågar appen efter serveradress och API-nyckel:

- Adress: `http://<ip-till-lxc-125>:8096`
- Nyckel: Jellyfin → **Instrumentpanel → API-nycklar → +**

Nyckeln sparas i `/config/config.json`. Vill du hellre sätta den som miljövariabler
använder du `JELLYFIN_URL` och `JELLYFIN_API_KEY` — då hoppas inloggningsrutan över.

### 5. Tillåt radering i Jellyfin

Användaren som äger API-nyckeln måste ha **Allow media deletion** påslaget för de
bibliotek du vill rensa (Jellyfin → Användare → Profil → Radering). Utan det svarar
servern 403 och JellyDupe visar vilka filer som misslyckades.

## Använda

| Vad | Var |
|---|---|
| Byta mellan filmer och avsnitt | sidopanelen |
| Söka på titel eller serie | sökfältet |
| Läsa om biblioteket efter ändringar | **Skanna om** |
| Se hur mycket som frigörs | siffran nere till vänster, uppdateras live |
| Testa utan att radera | kryssa **Testkör utan att radera** i bekräftelserutan |

Radering går via `DELETE /Items/{id}` i Jellyfin, vilket tar bort filen från disken.
Det finns ingen papperskorg — kör testkörningen först om du är osäker.

## Filer

```
jellydupe/
├── app/
│   ├── main.py          API, skanningsstatus, radering, omslagsproxy
│   ├── jellyfin.py      Jellyfin-klient och dubblettlogik
│   └── static/
│       └── index.html   hela gränssnittet
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

Vill du köra utan Docker under utveckling:

```bash
pip install -r requirements.txt
JELLYDUPE_CONFIG=./config uvicorn app.main:app --port 8095
```
