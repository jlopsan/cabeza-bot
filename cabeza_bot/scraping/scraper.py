"""Fachada de scraping — re-exporta los submódulos por fuente.
El código real vive en base/autoscout24/mobile_de/wallapop/coches_net/comparables.
Se mantiene para que los imports existentes (from ...scraper import X) sigan funcionando."""
from cabeza_bot.scraping.base import *  # noqa: F401,F403
from cabeza_bot.scraping.base import (  # underscore names no cubiertos por *
    _parse_numero, _generar_id, _nuevo_contexto_stealth, _postfiltrar,
    _persistir_de_historico, _PLAYWRIGHT_SEM,
)
from cabeza_bot.scraping.autoscout24 import ScraperAutoScout24  # noqa: F401
from cabeza_bot.scraping.mobile_de import ScraperMobileDe  # noqa: F401
from cabeza_bot.scraping.wallapop import ScraperWallapop  # noqa: F401
from cabeza_bot.scraping.coches_net import ScraperCochesNet  # noqa: F401
from cabeza_bot.scraping.comparables import *  # noqa: F401,F403
