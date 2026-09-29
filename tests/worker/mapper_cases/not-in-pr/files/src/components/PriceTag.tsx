import { formatPrice } from '../utils/format'

export function PriceTag({ cents }: { cents: number }) {
  return <span className="price">{formatPrice(cents)}</span>
}
