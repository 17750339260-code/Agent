app_key="1001300033"
secret_key="24e74daf74124b0b96c9cb113162a976"
url="https://10.10.65.213:18300/ai-inference-gateway/predict"

export LANG=en_US.UTF-8

curl_date=`date -u '+%a, %d %b %Y %T GMT'`
echo $curl_date
date_str="x-date: ${curl_date}"
date_base=`echo -n ${date_str} |openssl dgst -sha256 -hmac ${secret_key} -binary | base64`
curl_authorization='hmac username="'${app_key}'", algorithm="hmac-sha256", headers="x-date", signature="'${date_base}'"'
echo $curl_authorization

curl --insecure -k -vi -X POST ${url} -H "x-date: ${curl_date}" -H "authorization: ${curl_authorization}" --header 'Content-Type: application/json' \
--data '{
"componentCode":"04100525",
"model":"bge-reranker-v2-m3",
"query": "机器学习的应用领域",
"texts": [
       "机器学习在图像识别领域有广泛应用",
       "自然语言处理是机器学习的另一个重要应用",
       "天气预报主要依赖于气象卫星数据",
       "推荐系统使用机器学习算法为用户推荐商品",
       "机器学习在医疗诊断中也有重要应用"
     ],
     "top_n": 3
}'
