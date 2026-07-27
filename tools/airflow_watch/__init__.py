"""airflow-watch — a windowed monitor for Airflow deployments on Astro.

The spine is the investigation loop: see what's failing, drill into the failed
task, read its log. Reaches Airflow and Astro exclusively by shelling out to
the `astro` CLI (see `astro.py`), and knows about exactly one Airflow API
version — Airflow 2's — behind the seam in `api.py`.
"""
