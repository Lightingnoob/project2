/** Tailwind build config for assets/tailwind.css (rebuild after changing classes — see README). */
module.exports = {
  darkMode: 'class',
  content: ['./index.html', './ui.js'],
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'Helvetica Neue', 'Arial', 'sans-serif'] }
    }
  }
};
