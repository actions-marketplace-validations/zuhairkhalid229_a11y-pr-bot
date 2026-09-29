/** @jsxImportSource @emotion/react */
import { css } from '@emotion/react'
import hero from '../assets/hero-mountains.jpg'

const wrap = css`
  display: grid;
  place-items: center;
`

export default function Hero() {
  return (
    <section css={wrap}>
      <img src={hero} css={css`max-width: 100%`} />
      <h1>Climb higher</h1>
    </section>
  )
}
