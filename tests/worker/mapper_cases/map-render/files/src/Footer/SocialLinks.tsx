const socials = [
  { href: 'https://twitter.com/acme', icon: TwitterIcon },
  { href: 'https://github.com/acme', icon: GithubIcon },
  { href: 'https://linkedin.com/company/acme', icon: LinkedinIcon },
]

export function SocialLinks() {
  return (
    <ul className="social-links">
      {socials.map(({ href, icon: Icon }) => (
        <li key={href}>
          <a href={href} target="_blank" rel="noreferrer">
            <Icon />
          </a>
        </li>
      ))}
    </ul>
  )
}
