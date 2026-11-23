"""Team tables for the NFL and NBA verticals (D-020), keyed by Kalshi team UUID.

Each entry: Kalshi market code, Kalshi `custom_strike` team UUID, the schedule source's
abbreviation (nflverse for the NFL, ESPN via sportsdataverse for the NBA) and Kalshi's
label. Collected 2026-11-23 from public Kalshi markets (open and settled) and the two
schedule files. Kalshi labels drift; identity uses the UUID and the code, never the label.
Exhibition opponents (for example the London Lions, `LIO`) are deliberately absent, so
their games are never eligible.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Team:
    code: str  # Kalshi market code (ticker suffix)
    uuid: str  # Kalshi custom_strike team id
    source: str  # schedule source abbreviation
    label: str  # Kalshi yes_sub_title when collected (informational)


NFL_TEAMS = (
    Team("ARI", "d73dae4f-1f08-4b5e-bf23-4e25e9b6a69c", "ARI", "Arizona"),
    Team("ATL", "75685be6-4388-4bba-8bcb-d0b0c24feb50", "ATL", "Atlanta"),
    Team("BAL", "a69a57af-f1a1-4d44-ab62-e220ebaa2a5c", "BAL", "Baltimore"),
    Team("BUF", "f9acd396-ba35-4cae-a431-a44b2af707b0", "BUF", "Buffalo"),
    Team("CAR", "e37050ce-354f-4dd0-b2d8-0bd1e9650eb7", "CAR", "Carolina"),
    Team("CHI", "9a77500a-1945-495e-b6f5-5c35bedaa8b9", "CHI", "Chicago"),
    Team("CIN", "b007a1ab-bca6-42c7-8706-62607a605866", "CIN", "Cincinnati"),
    Team("CLE", "05cc62a4-bd74-4b2c-ab7b-1b177b9f6790", "CLE", "Cleveland"),
    Team("DAL", "62b97b7c-5503-461c-b328-fd8543219b22", "DAL", "Dallas"),
    Team("DEN", "0aa02fd7-1bb1-474b-98e1-5379d0a191e3", "DEN", "Denver"),
    Team("DET", "f95a55a7-8472-4ffa-be20-14547e3c32ff", "DET", "Detroit"),
    Team("GB", "08c52d1f-7ce1-445f-be72-97de0554ecc8", "GB", "Green Bay"),
    Team("HOU", "96edbad3-1be5-4528-a3ed-c6e0c28c5fa0", "HOU", "Houston"),
    Team("IND", "fe5f8219-fef4-4498-83d5-f6cc6f2cb7bf", "IND", "Indianapolis"),
    Team("JAC", "61a75ab7-eaf0-4601-b920-86b1e165007d", "JAX", "Jacksonville"),
    Team("KC", "64f72720-2e4a-4cc8-a39b-ca148aecb389", "KC", "Kansas City"),
    Team("LAC", "611e3786-3bfe-4919-bbd5-bc70fc13d827", "LAC", "Los Angeles C"),
    Team("LAR", "422d5fd7-ad18-4c96-902d-6f7c50e4660f", "LA", "Los Angeles R"),
    Team("LV", "8b1ce23e-7d64-4213-85e9-7d12bc8c4d85", "LV", "Las Vegas"),
    Team("MIA", "4cba614d-2d32-48ef-a066-1524437fbaff", "MIA", "Miami"),
    Team("MIN", "2279efa0-9284-4224-9647-09b14f6983ce", "MIN", "Minnesota"),
    Team("NE", "d1d64188-5cd5-4feb-97ff-8542c9dbeb43", "NE", "New England"),
    Team("NO", "f7c2cd06-61bf-469e-984f-3ef56cb8f3d7", "NO", "New Orleans"),
    Team("NYG", "4fa3f3ec-f7e7-41ec-9383-a40af2d26297", "NYG", "New York G"),
    Team("NYJ", "ccc2af2d-5c4d-4717-8036-5ba6bec5cfbd", "NYJ", "New York J"),
    Team("PHI", "bc65405b-5bda-425c-b7c5-681c382e6a5a", "PHI", "Philadelphia"),
    Team("PIT", "6aa76797-7833-45e1-85e3-96e0122801e1", "PIT", "Pittsburgh"),
    Team("SEA", "8e52cc75-cba8-4373-9b62-b535b53b3be8", "SEA", "Seattle"),
    Team("SF", "bfaa7639-8bdf-47b2-8d28-98d2cdbefbcb", "SF", "San Francisco"),
    Team("TB", "1105fa0d-c798-4020-9e24-f377a334b3d9", "TB", "Tampa Bay"),
    Team("TEN", "7c86629b-eafc-47b4-9204-b46a2d0e9cbb", "TEN", "Tennessee"),
    Team("WAS", "aa65dda6-9cb8-4c8b-812b-d0c1feaae54f", "WAS", "Washington"),
)

NBA_TEAMS = (
    Team("ATL", "e0729403-9c95-4ee2-b5d4-f704d8322570", "ATL", "Atlanta"),
    Team("BKN", "7cc1eb26-46ee-44b7-94ad-248ee3b0fbd3", "BKN", "Brooklyn"),
    Team("BOS", "6dd427f5-fa02-40ef-aa90-cfa73e0c539e", "BOS", "Boston"),
    Team("CHA", "7c980c51-fa76-4571-b165-242b2a00f3a1", "CHA", "Charlotte"),
    Team("CHI", "65f0250b-455d-43d7-b7dc-7d321bdf9abc", "CHI", "Chicago"),
    Team("CLE", "90381a35-42a6-4975-851f-175418e4271a", "CLE", "Cleveland"),
    Team("DAL", "36547ae6-f0fa-4d06-a6e5-5bfa64a217ed", "DAL", "Dallas"),
    Team("DEN", "ba2cb6be-9827-46a8-8e6f-d8661307ba77", "DEN", "Denver"),
    Team("DET", "65569528-1888-40aa-9d62-2b0e8142a9b4", "DET", "Detroit"),
    Team("GSW", "502c629b-4da7-40fd-92ab-20cb55411c35", "GS", "Golden State"),
    Team("HOU", "a8625e4b-4ae6-4062-a7c1-0b6e1f4ae211", "HOU", "Houston"),
    Team("IND", "d059bab2-9fb5-42dd-8038-5a12474300ce", "IND", "Indiana"),
    Team("LAC", "daca4413-0ea1-4db2-a815-f05c181ef0a6", "LAC", "Los Angeles C"),
    Team("LAL", "2ef4d31c-0b46-4f43-a403-f44d62489034", "LAL", "Los Angeles L"),
    Team("MEM", "1cfe70b8-4dfc-47dc-acca-b68d4a725972", "MEM", "Memphis"),
    Team("MIA", "a17e4a68-2999-4f60-b67e-3e80aa9e198b", "MIA", "Miami"),
    Team("MIL", "5dc3fe01-79a4-4f2f-a953-d922ea4a6dd3", "MIL", "Milwaukee"),
    Team("MIN", "29e3c04e-fadb-4006-be20-cbb8c159ce0e", "MIN", "Minnesota"),
    Team("NOP", "d3c8c2ac-c4a0-4d67-8b16-53cb91118dfc", "NO", "New Orleans"),
    Team("NYK", "67468ecc-b868-43a4-b9dc-751a52894bb0", "NY", "New York"),
    Team("OKC", "a85f6eca-2f3b-4f61-83ab-8d049f59ce2c", "OKC", "Oklahoma City"),
    Team("ORL", "00c3e69d-d9b9-4ac3-bf79-19707fe43637", "ORL", "Orlando"),
    Team("PHI", "0554528d-586b-461f-827b-801cf93e8a8e", "PHI", "Philadelphia"),
    Team("PHX", "ace11dff-c5d1-4ab9-9452-5312253659fd", "PHX", "Phoenix"),
    Team("POR", "2229b01e-68eb-46ad-adf4-71a9533c84fc", "POR", "Portland"),
    Team("SAC", "ab9e2e9a-3243-41b6-a291-95f4d9c6edf7", "SAC", "Sacramento"),
    Team("SAS", "ad36c3e8-4194-4e63-920f-7c50f46191a6", "SA", "San Antonio"),
    Team("TOR", "96c447dd-2cdd-4c58-a151-ef0df87de981", "TOR", "Toronto"),
    Team("UTA", "6f60dfb1-fda1-423f-b76f-67e9d24b9644", "UTAH", "Utah"),
    Team("WAS", "0464e75c-8bc8-4f18-9ee2-577832b889d6", "WSH", "Washington"),
)
