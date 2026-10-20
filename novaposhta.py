"""Пошук населених пунктів через API Нової Пошти."""
import logging
import os
from typing import Dict, List

import aiohttp

API_URL = "https://api.novaposhta.ua/v2.0/json/"


async def search_cities(query: str, limit: int = 10) -> List[Dict[str, str]]:
    """Повертає список {"display": "Місто (Район, Область)", "value": "Місто"}."""
    api_key = os.getenv("NOVAPOSHTA_API_KEY")
    if not api_key:
        logging.error("NOVAPOSHTA_API_KEY не встановлено")
        return []

    payload = {
        "apiKey": api_key,
        "modelName": "Address",
        "calledMethod": "searchSettlements",
        "methodProperties": {
            "CityName": query,
            "Limit": str(limit),
        },
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(API_URL, json=payload) as response:
                data = await response.json(content_type=None)
    except Exception as e:
        logging.error(f"Error searching cities: {e}")
        return []

    if not data.get("success"):
        return []

    addresses = (data.get("data") or [{}])[0].get("Addresses", [])
    results = []
    for addr in addresses:
        # Формуємо назву: "Місто (Район, Область)"
        present = addr.get("Present", "")
        area = addr.get("Area", "")
        region = addr.get("Region", "")

        if area and region:
            display = f"{present} ({area}, {region})"
        elif region:
            display = f"{present} ({region})"
        else:
            display = present

        results.append({"display": display, "value": present})

    return results[:limit]
