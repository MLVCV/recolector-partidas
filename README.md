# Recolector de partidas

Junta partidas ranked solo/duo de LAS del parche actual con la API oficial de Riot y calcula, por campeón y línea, partidas, WR, % de línea, pick rate, ban rate y tier.

Corre solo en GitHub Actions cada 6 horas (`.github/workflows/recolector.yml`). Cada corrida sigue desde la anterior y deja `lol_data.js` y `lol_data.json` para bajar en "Artifacts".

- La clave de Riot va como secreto del repo (`RIOT_API_KEY`). Nunca se sube al código.
- Por partida se guardan el código, la duración, el elo, los campeones con línea y resultado y algunas estadísticas de juego. No se guardan identificadores de jugadores.
- Las partidas quedan en la caché de Actions, que no es pública.

`recolector_riot.py` es una copia del que está en el repo principal del proyecto. Los cambios se hacen allá y se copian acá.

Aviso que piden las políticas de la API de Riot:

> This project isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
