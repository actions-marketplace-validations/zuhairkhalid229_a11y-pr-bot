export function Newsletter() {
  return (
    <form className="newsletter" action="/subscribe" method="post">
      <input type="email" name="email" placeholder="you@example.com" required />
      <button type="submit">Subscribe</button>
    </form>
  )
}
