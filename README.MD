

# how to create a postgres database on localhost
sudo apt install postgresql postgresql-contrib -y    
sudo systemctl enable postgresql.service; service postgresql start
sudo -u postgres -i   

CREATE ROLE lmex LOGIN PASSWORD 'postgres';    
CREATE database youtube_project ;