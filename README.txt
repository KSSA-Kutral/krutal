kutral

project structure:
  app.py
  templates/index.html
  static/map.js
  data/combined_2024-01-15.csv   (optional sample fallback)
  .env                           (created when you save your MAP_KEY)
  requirements.txt

install:
  pip install -r requirements.txt

run:
  python app.py

then open:
  http://127.0.0.1:5000

the apps setup panel saves FIRMS_MAP_KEY into a local .env file
pls dont upload or share your .env, its included in .gitignore

FIRMS date range:
- the interface supports up to 30 days per query
- the backend splits requests into chunks of at most 5 days, as required by the FIRMS Area API
- acquisition-time filters are applied using UTC
- if no MAP_KEY is configured, the optional local sample CSV is shown instead
