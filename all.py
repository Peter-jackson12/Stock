import pandas as pd
import os

# 파일 경로 설정
file_dir = r"C:\Users\user\PycharmProjects\CrossToday\fowardtest"
# 파일명 리스트 생성
file_list = ['cross_Today_1.csv', 'cross_Today_2.csv', 'cross_Today_3.csv']

# 데이터프레임 리스트 생성
df_list = []
for file_name in file_list:
    file_path = os.path.join(file_dir, file_name)
    df = pd.read_csv(file_path, encoding='euc-kr')
    df_list.append(df)

# 데이터프레임 병합
result = pd.concat(df_list)

# 병합된 결과를 csv 파일로 저장
result.to_csv(os.path.join(file_dir, 'total.csv'), encoding='euc-kr', index=False)
