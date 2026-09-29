import { Badge } from './Badge'

export function Card({ title, image, isNew }: Props) {
  return (
    <article className="card">
      <img src={image} className="card-img" />
      <h3>{title}</h3>
      {isNew && <Badge tone="info">New</Badge>}
    </article>
  )
}
