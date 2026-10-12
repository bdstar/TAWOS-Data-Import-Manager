-- MySQL dump 10.13  Distrib 8.0.30, for macos12 (x86_64)
--
-- Host: localhost    Database: tawos_fixture
-- ------------------------------------------------------
-- Server version	8.0.21

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `Affected_Version`
--

DROP TABLE IF EXISTS `Affected_Version`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `Affected_Version` (
  `Issue_ID` int DEFAULT NULL,
  `Affected_Version_ID` int DEFAULT NULL,
  KEY `Affected_Version_ID` (`Affected_Version_ID`),
  KEY `F_Key` (`Issue_ID`,`Affected_Version_ID`),
  CONSTRAINT `affected_version_ibfk_1` FOREIGN KEY (`Issue_ID`) REFERENCES `Issue` (`ID`),
  CONSTRAINT `affected_version_ibfk_2` FOREIGN KEY (`Affected_Version_ID`) REFERENCES `Version` (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `Affected_Version`
--

LOCK TABLES `Affected_Version` WRITE;
/*!40000 ALTER TABLE `Affected_Version` DISABLE KEYS */;
INSERT INTO `Affected_Version` VALUES (100,10),(102,11);
INSERT INTO `Affected_Version` VALUES (103,10);
/*!40000 ALTER TABLE `Affected_Version` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `Change_Log`
--

DROP TABLE IF EXISTS `Change_Log`;
CREATE TABLE `Change_Log` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `Field` varchar(512) DEFAULT NULL,
  `From_Value` mediumtext,
  `To_Value` mediumtext,
  `From_String` mediumtext,
  `To_String` mediumtext,
  `Change_Type` varchar(512) DEFAULT NULL,
  `Creation_Date` datetime DEFAULT NULL,
  `Author_ID` int DEFAULT NULL,
  `Issue_ID` int DEFAULT NULL,
  PRIMARY KEY (`ID`),
  CONSTRAINT `change_log_ibfk_1` FOREIGN KEY (`Issue_ID`) REFERENCES `Issue` (`ID`),
  CONSTRAINT `change_log_ibfk_2` FOREIGN KEY (`Author_ID`) REFERENCES `User` (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Change_Log` WRITE;
INSERT INTO `Change_Log` VALUES (1,'status','1','2','Open','Closed','STATUS','2013-05-03 00:00:00',1,100),(2,'description',NULL,NULL,'old ),( text','new; text','OTHER','2013-05-04 00:00:00',2,101);
INSERT INTO `Change_Log` VALUES (3,'Rank','','','','Ranked higher','OTHER','2013-05-05 00:00:00',NULL,103);
UNLOCK TABLES;

--
-- Table structure for table `Comment`
--

DROP TABLE IF EXISTS `Comment`;
CREATE TABLE `Comment` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `Comment` mediumtext,
  `Comment_Text` mediumtext,
  `Comment_Code` mediumtext,
  `Creation_Date` datetime DEFAULT NULL,
  `Author_ID` int DEFAULT NULL,
  `Issue_ID` int DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Comment` WRITE;
INSERT INTO `Comment` VALUES (1,'Comment with ; semicolon and \'quotes\'','Comment with ; semicolon and quotes',NULL,'2013-05-02 00:00:00',2,100),(2,'Multi
line raw','Multi\nline escaped','{code}x = \"y\";{code}','2013-05-03 00:00:00',3,101),(3,'',NULL,NULL,NULL,NULL,102);
UNLOCK TABLES;

--
-- Table structure for table `Component`
--

DROP TABLE IF EXISTS `Component`;
CREATE TABLE `Component` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `Jira_ID` int DEFAULT NULL,
  `Name` varchar(512) DEFAULT NULL,
  `Description` varchar(512) DEFAULT NULL,
  `Project_ID` int DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Component` WRITE;
INSERT INTO `Component` VALUES (5,501,'core','Core -- component /* not a comment */',1),(6,502,'ui',NULL,2);
UNLOCK TABLES;

--
-- Table structure for table `Fix_Version`
--

DROP TABLE IF EXISTS `Fix_Version`;
CREATE TABLE `Fix_Version` (
  `Issue_ID` int DEFAULT NULL,
  `Fix_Version_ID` int DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Fix_Version` WRITE;
INSERT INTO `Fix_Version` VALUES (100,10),(101,10),(101,11);
UNLOCK TABLES;

--
-- Table structure for table `Issue`
--

DROP TABLE IF EXISTS `Issue`;
CREATE TABLE `Issue` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `Jira_ID` int DEFAULT NULL,
  `Issue_Key` varchar(512) DEFAULT NULL,
  `URL` varchar(512) DEFAULT NULL,
  `Title` varchar(512) DEFAULT NULL,
  `Description` mediumtext,
  `Description_Text` mediumtext,
  `Description_Code` mediumtext,
  `Type` varchar(128) DEFAULT NULL,
  `Priority` varchar(128) DEFAULT NULL,
  `Status` varchar(128) DEFAULT NULL,
  `Resolution` varchar(128) DEFAULT NULL,
  `Creation_Date` datetime DEFAULT NULL,
  `Estimation_Date` datetime DEFAULT NULL,
  `Resolution_Date` datetime DEFAULT NULL,
  `Last_Updated` datetime DEFAULT NULL,
  `Story_Point` double DEFAULT NULL,
  `Timespent` double DEFAULT NULL,
  `In_Progress_Minutes` double DEFAULT NULL,
  `Total_Effort_Minutes` double DEFAULT NULL,
  `Resolution_Time_Minutes` double DEFAULT NULL,
  `Title_Changed_After_Estimation` tinyint DEFAULT NULL,
  `Description_Changed_After_Estimation` tinyint DEFAULT NULL,
  `Story_Point_Changed_After_Estimation` tinyint DEFAULT NULL,
  `Pull_Request_URL` varchar(512) DEFAULT NULL,
  `Creator_ID` int DEFAULT NULL,
  `Reporter_ID` int DEFAULT NULL,
  `Assignee_ID` int DEFAULT NULL,
  `Project_ID` int DEFAULT NULL,
  `Sprint_ID` int DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=105 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Issue` WRITE;
INSERT INTO `Issue` VALUES (100,77638,'XD-1','https://jira.spring.io/browse/XD-1','Title with \'quote\' and \"double\"','Line1\r\nLine2; path C:\\temp\\new ),( done','plain text','{code}a;b{code}','Story','Major','Closed','Fixed','2013-05-01 10:00:00',NULL,'2013-06-01 10:00:00','2013-06-02 10:00:00',3,NULL,120.5,240,1440,0,0,0,NULL,1,1,2,1,1),(101,77639,'XD-2','https://jira.spring.io/browse/XD-2','It''s doubled','Emoji 🚀 and CJK 日本語 -- not a comment /* nor this */ # nor this',NULL,NULL,'Bug','Minor','Open',NULL,'2013-05-02 11:00:00',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'https://github.com/x/pull/1',2,2,NULL,1,NULL);
INSERT INTO `Issue` VALUES (102,1,'NEXUS-1','u','Negative and exp','Backslash-escapes: \0 tab\there \Z end',NULL,NULL,'Task','Major','Done','Fixed','2014-01-01 00:00:00',NULL,NULL,NULL,-1.5,1.5e3,0,0,0,1,1,1,NULL,3,3,3,2,NULL),(103,2,'NEXUS-2','u','Embedded raw newline','first line
second line',NULL,NULL,'Bug','Major','Open',NULL,'2014-01-02 00:00:00',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,3,NULL,NULL,2,NULL),(104,3,'NEXUS-3','u','Orphan creator',NULL,NULL,NULL,'Bug','Major','Open',NULL,'2014-01-03 00:00:00',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,999,NULL,NULL,2,NULL);
UNLOCK TABLES;

--
-- Table structure for table `Issue_Component`
--

DROP TABLE IF EXISTS `Issue_Component`;
CREATE TABLE `Issue_Component` (
  `Issue_ID` int DEFAULT NULL,
  `Component_ID` int DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Issue_Component` WRITE;
INSERT INTO `Issue_Component` VALUES (100,5),(101,5),(103,6);
UNLOCK TABLES;

--
-- Table structure for table `Issue_Link`
--

DROP TABLE IF EXISTS `Issue_Link`;
CREATE TABLE `Issue_Link` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `Issue_ID` int DEFAULT NULL,
  `Name` varchar(512) DEFAULT NULL,
  `Description` varchar(2048) DEFAULT NULL,
  `Direction` varchar(45) DEFAULT NULL,
  `Target_Issue_ID` int DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Issue_Link` WRITE;
INSERT INTO `Issue_Link` VALUES (1,100,'Relates','relates to','Outward',101);
INSERT IGNORE INTO `Issue_Link` (`ID`, `Issue_ID`, `Target_Issue_ID`, `Name`, `Description`, `Direction`) VALUES (2,101,100,'Blocks','is blocked by','Inward');
UNLOCK TABLES;

--
-- Table structure for table `Project`
--

DROP TABLE IF EXISTS `Project`;
CREATE TABLE `Project` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `Project_Key` varchar(128) DEFAULT NULL,
  `Name` varchar(512) DEFAULT NULL,
  `URL` varchar(512) DEFAULT NULL,
  `Description` varchar(2048) DEFAULT NULL,
  `Start_Date` datetime DEFAULT NULL,
  `Last_Update_Date` datetime DEFAULT NULL,
  `SP_Field_ID` varchar(45) DEFAULT NULL,
  `Repository_ID` int DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Project` WRITE;
INSERT INTO `Project` VALUES (1,'XD','Spring XD','https://jira.spring.io/projects/XD','Desc with \\n literal backslash-n and real\nnewline','2013-01-01 00:00:00','2020-01-01 00:00:00','customfield_10142',1),(2,'NEXUS','Sonatype Nexus','https://issues.sonatype.org','Unicode: émoji 🚀 日本語',NULL,NULL,'customfield_10132',2);
UNLOCK TABLES;

--
-- Table structure for table `Repository`
--

DROP TABLE IF EXISTS `Repository`;
CREATE TABLE `Repository` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `Name` varchar(512) CHARACTER SET utf8 COLLATE utf8_general_ci DEFAULT NULL,
  `Description` varchar(2048) CHARACTER SET utf8 COLLATE utf8_general_ci DEFAULT NULL,
  `URL` varchar(512) DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Repository` WRITE;
INSERT INTO `Repository` VALUES (1,'Spring','Spring repo; with semicolon','https://jira.spring.io'),(2,'Sonatype','It''s \"quoted\"','https://issues.sonatype.org');
UNLOCK TABLES;

--
-- Table structure for table `Sprint`
--

DROP TABLE IF EXISTS `Sprint`;
CREATE TABLE `Sprint` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `JiraID` int DEFAULT NULL,
  `Name` varchar(512) DEFAULT NULL,
  `State` varchar(512) DEFAULT NULL,
  `Start_Date` datetime DEFAULT NULL,
  `End_Date` datetime DEFAULT NULL,
  `Activated_Date` datetime DEFAULT NULL,
  `Complete_Date` datetime DEFAULT NULL,
  `Project_ID` int DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Sprint` WRITE;
INSERT INTO `Sprint` VALUES (1,101,'Sprint 1','CLOSED','2014-01-01 00:00:00',NULL,NULL,NULL,1);
UNLOCK TABLES;

--
-- Table structure for table `User`
--

DROP TABLE IF EXISTS `User`;
CREATE TABLE `User` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `Project_ID` int DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `User` WRITE;
INSERT INTO `User` VALUES (1,1),(2,1),(3,2);
UNLOCK TABLES;

--
-- Table structure for table `Version`
--

DROP TABLE IF EXISTS `Version`;
CREATE TABLE `Version` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `Jira_ID` int DEFAULT NULL,
  `Name` varchar(512) DEFAULT NULL,
  `Description` varchar(2048) DEFAULT NULL,
  `Archived` tinyint DEFAULT NULL,
  `Released` tinyint DEFAULT NULL,
  `Release_Date` datetime DEFAULT NULL,
  `Project_ID` int DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB AUTO_INCREMENT=12 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

LOCK TABLES `Version` WRITE;
INSERT INTO `Version` VALUES (10,1001,'1.0','First; release',1,1,'2014-02-01 00:00:00',1),(11,1002,'2.0',NULL,0,0,NULL,2);
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2022-08-01 10:00:00
