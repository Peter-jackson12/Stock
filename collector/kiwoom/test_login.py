import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QAxContainer import QAxWidget

class KiwoomLoginTester:
    def __init__(self):
        self.app = QApplication(sys.argv)
        try:
            self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        except Exception as e:
            print(f"❌ 키움 OCX 로드 실패: {e}")
            sys.exit(1)
        self.ocx.OnEventConnect.connect(self._on_login)

    def test(self):
        print("🔑 키움 OpenAPI+ 로그인 창을 띄웁니다...")
        self.ocx.dynamicCall("CommConnect()")
        self.app.exec_()

    def _on_login(self, err_code: int):
        if err_code == 0:
            print("\n" + "=" * 50)
            print("🎉 [성공] 키움증권 OpenAPI+ 로그인 및 통신 성공!")
            user_id = self.ocx.dynamicCall("GetLoginInfo(QString)", "USER_ID")
            user_name = self.ocx.dynamicCall("GetLoginInfo(QString)", "USER_NAME")
            acc_cnt = self.ocx.dynamicCall("GetLoginInfo(QString)", "ACCOUNT_CNT")
            acc_list = self.ocx.dynamicCall("GetLoginInfo(QString)", "ACCLIST")
            print(f"👤 사용자 ID: {user_id} ({user_name})")
            print(f"🏦 보유 계좌 수: {acc_cnt}개 (계좌목록: {acc_list})")
            print("=" * 50)
        else:
            print(f"❌ 로그인 실패 (에러코드: {err_code})")
        self.app.quit()

if __name__ == "__main__":
    tester = KiwoomLoginTester()
    tester.test()
