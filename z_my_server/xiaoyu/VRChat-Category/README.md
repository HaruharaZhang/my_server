# VRChat Photography Portfolio

A multi-page photography portfolio built with Astro 7. The current version uses
generated placeholders while the final VRChat photography is being prepared.

## Pages

- `/` — Home
- `/about` — About
- `/portfolio` — Portfolio sections for ME, Together, Worlds, and Friends
- `/movie` — Moving image archive

## Project structure

```text
/
├── public/                 # Static assets
├── src/
│   ├── components/         # Reusable UI components
│   ├── data/               # Shared portfolio metadata
│   ├── layouts/            # Shared page shell
│   ├── pages/              # File-based routes
│   └── styles/             # Global styles
├── astro.config.mjs
└── package.json
```

## Commands

All commands are run from the project root:

| Command                   | Action                                           |
| :------------------------ | :----------------------------------------------- |
| `npm install`             | Install dependencies                             |
| `npm run dev`             | Start the local development server               |
| `npm run build`           | Build the production site to `./dist/`           |
| `npm run preview`         | Preview the production build locally             |
| `npm run astro ...`       | Run Astro CLI commands                           |
| `npm run astro -- --help` | Show Astro CLI help                              |
