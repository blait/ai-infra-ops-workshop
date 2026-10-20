# AI 기반 AWS 인프라 운영 워크샵 — 웹 가이드

워크샵 열기: https://blait.github.io/ai-infra-ops-workshop/

이 저장소는 고객에게 제공하는 웹 가이드의 게시용 저장소입니다.

- `index.html`: 본문, 화면 코드, 스크린샷을 포함한 단일 HTML
- `.nojekyll`: 생성한 파일을 그대로 제공하는 GitHub Pages 설정
- `README.md`: 게시 안내

GitHub Pages는 `main` 브랜치의 루트(`/`)를 게시합니다.
`main`에 변경을 푸시하면 사이트가 갱신됩니다.

본문과 스크린샷은 원본 작업 저장소의 `workshop/pages/`에서 수정합니다.
원본 저장소 루트에서 다음 명령을 실행하면 strict 빌드, 이 저장소의 게시 파일
업데이트, 푸시, 공개 사이트 HTML 검증을 순서대로 수행합니다.

```bash
python3 workshop/publish_pages.py --publish
```

이 웹사이트는 실습 안내서입니다. 모듈 3의 Streamlit 앱은 가이드에 따라 로컬에서
실행하며, 에이전트 백엔드는 AWS AgentCore를 사용합니다.

## 모듈 3 데모 코드

모듈 3 데모 코드: `demo/ai-ops-slack-agent/` — 실행 방법은 웹 가이드 40-04 페이지 참고
