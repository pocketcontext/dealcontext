function serve(e, file) {
  const h = e.response.header();
  h.set('Cache-Control', file === 'index.html' ? 'no-store' : 'public, max-age=31536000, immutable');
  h.set('Referrer-Policy', 'no-referrer');
  h.set('X-Content-Type-Options', 'nosniff');
  h.set('X-Frame-Options', 'DENY');
  h.set('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
  return e.fileFS($os.dirFS('ui/dist'), file);
}
module.exports = {serve};
