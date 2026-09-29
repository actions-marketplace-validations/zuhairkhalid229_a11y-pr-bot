import { IconButton } from './IconButton'
import { BoldIcon, ItalicIcon } from './icons'

export function Toolbar() {
  return (
    <div className="toolbar">
      <IconButton data-testid="fmt-bold" icon={<BoldIcon />} label="Bold" />
      <IconButton data-testid="fmt-italic" icon={<ItalicIcon />} />
    </div>
  )
}
