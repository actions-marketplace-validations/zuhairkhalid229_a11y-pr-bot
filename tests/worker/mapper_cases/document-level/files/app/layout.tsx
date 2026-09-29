import './globals.css'

export const metadata = { title: 'Acme' }

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html>
      <body className="antialiased">{children}</body>
    </html>
  )
}
