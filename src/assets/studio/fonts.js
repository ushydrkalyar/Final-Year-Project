// The bundled fonts (Google Fonts, OFL), registered for the panel and the CSS stand-ins.

export async function load() {
  try {
    const response = await fetch('/fonts/fonts.json');
    if (!response.ok) return [];
    const families = await response.json();
    const faces = families.flatMap(f => f.files.map(file => {
      const weight = file.variable ? '100 900' : file.weight;
      return `@font-face{font-family:'${f.family}';src:url('/fonts/${file.file}');font-weight:${weight};font-style:${file.italic ? 'italic' : 'normal'};font-display:swap}`;
    }));
    document.head.append(Object.assign(document.createElement('style'), { textContent: faces.join('') }));
    return families.map(f => f.family);
  } catch {
    return [];
  }
}
