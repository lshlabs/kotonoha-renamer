# Kotonoha Renamer 빌드·배포

배포 대상은 `kotonoha-renamer-v1.0.1-windows-x64.zip`과 체크섬입니다. 설치 프로그램은 새 빌드에서 생성하지 않습니다.

```powershell
python -m pip install -r requirements-build.txt
python -m unittest discover
.\build.ps1 -Python python
```

Python 실행 환경·HTML 화면·파일 작업 엔진을 ZIP에 포함합니다. pywebview와 WebView2 의존성은 제거했습니다. Windows 폴더·로그 선택 대화상자를 위해 Python 기본 Tk 런타임을 포함합니다.

패키징은 실행 파일 버전과 ZIP 무결성을 검사하고 SHA-256을 생성합니다. 실행 중 사용한 `data` 내용, 개인 설정, 모델은 ZIP에 넣지 않습니다. 배포 파일은 코드 서명되지 않았습니다.

저장소는 `lshlabs/kotonoha-renamer`를 사용합니다. 소스는 저장소 루트에 배치하고, `build/`·`dist/`·`data/`·Python 가상환경은 Git에서 제외합니다.

빌드 결과는 `dist/kotonoha-renamer/`에 생성됩니다. ZIP 내부 최상위 폴더도 `kotonoha-renamer/`이며, `Kotonoha.exe`와 `_internal`을 함께 배포합니다. 실행 파일은 `Kotonoha.exe` 하나이며 CLI는 포함하지 않습니다.

GitHub Release 태그는 코드 버전과 같은 `v1.0.1`을 사용하고 정식 릴리스로 표시합니다. ZIP과 SHA256SUMS만 첨부하고 개인 데이터나 빌드 캐시는 포함하지 않습니다. GitHub가 소스 ZIP을 별도로 제공합니다.

배포본의 설치·실행 환경과 알려진 한계는 검증 보고서와 릴리스 설명에 기록합니다.
