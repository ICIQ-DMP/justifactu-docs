# justifactu-docs

ProperDocs documentation for [justifactu](https://github.com/ICIQ-DMP/justifactu) —
software to automate billing justification at ICIQ.

Published at <https://iciq-dmp.github.io/justifactu/>.

## Syncing template changes

Use GitHub's **Sync fork** button (or `git merge upstream/master` locally).  
The only expected conflict is the PROJECT-SPECIFIC block at the top of `properdocs.yml`.

## Structure

```
docs/
├── index.md            # Home page
├── tutorials/          # Learning-oriented content (write by hand)
├── how-to/             # Task-oriented content (write by hand)
├── reference/          # Auto-generated from source docstrings
├── explanation/        # Understanding-oriented content (write by hand)
└── gen_ref_pages.py    # mkdocs-gen-files script for reference generation
properdocs.yml
requirements.txt
.github/workflows/
└── deploy-docs.yml     # Build + deploy to GitHub Pages
```

## Local development

**With Docker (recommended):**

```bash
git clone https://github.com/ICIQ-DMP/justifactu source
docker compose up
```

Open <http://localhost:8000>.

**Without Docker:**

```bash
pip install -r requirements.txt
git clone https://github.com/ICIQ-DMP/justifactu source
properdocs serve
```

## Theme overrides

Place custom Jinja2 templates in `docs/overrides/` to extend or replace
Material for MkDocs partials. `docs/overrides/main.html` is included as a
starting point — it simply extends the base template.

## Mermaid diagrams

Use fenced code blocks tagged `mermaid`:

````markdown
```mermaid
graph LR
    A --> B --> C
```
````

## Redirects

To redirect a moved page, add entries to the `redirects.redirect_maps` block in
`properdocs.yml`:

```yaml
plugins:
  - redirects:
      redirect_maps:
        'old/page.md': 'new/page.md'
```
