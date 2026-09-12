# MatGUI — Matdata

A simple, clean website to host **MatGUI** (a modern knowledge graph and SPARQL workbench, formerly *Yasgui*) without Node.js or Docker dependencies. This is a pure HTML/CSS/JavaScript implementation that can be deployed easily to any static hosting service.

🌐 **Live Demo**: [https://matgui.matdata.eu](https://matgui.matdata.eu)

## Features

- Clean and minimal MatGUI interface
- No build process required - just HTML, CSS, and JavaScript
- Easy deployment to GitHub Pages or any static hosting
- Pre-configured with popular SPARQL endpoints
- Extended with graph visualization and geo plugins

## Built With

- [Matdata-eu/MatGUI](https://github.com/Matdata-eu/MatGUI) - A modern knowledge graph and SPARQL workbench
- [Matdata-eu/Yasgui-table-plugin](https://github.com/Matdata-eu/Yasgui-table-plugin) - Tabular data visualization plugin
- [Matdata-eu/Yasgui-graph-plugin](https://github.com/Matdata-eu/Yasgui-graph-plugin) - Graph visualization plugin
- [Matdata-eu/Yasgui-geo-plugin](https://github.com/Matdata-eu/Yasgui-geo-plugin) - Geographic data visualization plugin

## Default SPARQL Endpoints

The application comes pre-configured with the following SPARQL endpoints:

- **ERA (European Railway Agency)**: `https://data-interop.era.europa.eu/api/sparql`
- **DBpedia**: `https://dbpedia.org/sparql`
- **Wikidata**: `https://query.wikidata.org`

## Deployment Example

This project demonstrates how to deploy MatGUI without Node.js or Docker - just plain HTML files served statically.

### Deploy to GitHub Pages

1. Fork or clone this repository
2. Enable GitHub Pages in repository settings
3. Set the source to the `main` branch and `/src` folder (or use the included GitHub Actions workflow)
4. Your MatGUI instance will be available at `https://yourusername.github.io/repository-name/`

### Deploy to Any Static Host

Simply upload the contents of the `src/` folder to any static web hosting service:

```bash
# Contents of src/ folder:
- index.html      # Main HTML file
- main.js         # MatGUI configuration
- main.css        # Custom styles
- logo.svg        # Matdata logo
- favicon.png     # Site favicon
- manifest.json   # PWA manifest
- robots.txt      # Search engine instructions
```

No build step required! The files can be served directly.

## Local Development

To run locally, simply open `src/index.html` in a web browser, or use any local static server:

```bash
# Using Python
cd src
python -m http.server 8000

# Using PHP
cd src
php -S localhost:8000

# Using Node.js (http-server)
cd src
npx http-server
```

Then visit `http://localhost:8000` in your browser.

## Customization

### Adding SPARQL Endpoints

Edit `src/main.js` and add endpoints to the `endpointCatalogueOptions.getData()` array:

```javascript
{
  endpoint: "https://your-sparql-endpoint.com/sparql",
}
```

### Styling

Modify `src/main.css` to customize the appearance of the interface.

### Configuration

The MatGUI instance can be configured in `src/main.js`. See the [MatGUI documentation](https://matgui-doc.matdata.eu) for available options.

## Credits

- Original YASGUI by [Zazuko](https://github.com/zazuko/yasgui) and [TriplyDB](https://github.com/triplydb/Yasgui)
- Enhanced by [Matdata-eu](https://github.com/Matdata-eu)

## License

See [LICENSE](LICENSE) file for details.
