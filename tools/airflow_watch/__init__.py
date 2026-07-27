"""airflow-watch — a windowed monitor for Airflow deployments on Astro.

The spine is the investigation loop: see what's failing, drill into the failed
task, read its log. Reaches Airflow and Astro exclusively by shelling out to
the `astro` CLI (see `astro.py`), and speaks two Airflow API versions —
Airflow 2's and Airflow 3's — behind the single seam in `api.py`, refusing any
other major by name.
"""
