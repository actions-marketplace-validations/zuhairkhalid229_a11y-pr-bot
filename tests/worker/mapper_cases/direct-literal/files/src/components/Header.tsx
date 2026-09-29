import Link from 'next/link'

export function Header() {
  return (
    <header className="flex items-center gap-4 px-6 py-3">
      <Link href="/">
        <img src="/logo.svg" className="h-8 w-auto" width={120} height={32} />
      </Link>
      <nav className="ml-auto">
        <Link href="/pricing">Pricing</Link>
      </nav>
    </header>
  )
}
