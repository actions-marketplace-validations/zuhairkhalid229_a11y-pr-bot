import { useTranslation } from 'react-i18next'
import { SpinnerIcon } from '../icons'

export function SubmitButton({ busy }: { busy: boolean }) {
  const { t } = useTranslation()
  return (
    <button type="submit" className="btn-primary" disabled={busy}>
      {busy ? <SpinnerIcon /> : null}
    </button>
  )
}
